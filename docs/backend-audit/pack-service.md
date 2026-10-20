# Pack-Service Audit — `pihub/backend/pack-service`

**Scope:** `app/main.py` + `app/pack_generator.py` (788) + `app/pack_storage/*` + `app/sync/*` + `app/api/*` + `app/semantic_content_pipeline.py` + `app/validation/*`  
**Date:** 2026-09-12

---

## 1. Duplicate Logic

| ID | File:Line | Finding | Fix |
|---|---|---|---|
| 1.1 | `pack_generator.py:622-625` `_pack_id_part` vs `pack_storage/storage_layout.py:11-19` `slugify` vs `app/main.py:105-123` `_expected_pack_id` vs `content-pipeline/app/main.py:41-47` `_sanitize_pack_id` | 4× same `[^a-z0-9]+→_` slug — `main.py:110` default `language or "english"` diverges from `pack_generator:105` `normalize_language_code` → expected pack ID mismatched cleanup on `PackGenerationNoContentError`. | Single `shared.text_normalization` helper. |
| 1.2 | `pack_generator.py:404-444` `_build_qdrant_filter` + `310-319` `_normalize_metadata_value` + `SUBJECT_QUERY_ALIASES:37` | Duplicates `shared/vector_store.build_filter`; aliases `maths→mathematics` not in shared. | Use shared. |
| 1.3 | `pack_generator:81-132` `generate_class_pack`/`generate_chapter_pack`/`generate_language_pack` + `662-736` `_create_pack` | 90% identical normalize→`_search`→`_ensure_chunks`→`_create_pack`; second duplicates `content-pipeline/pack_compiler.py:18`. | One `generate(pack_type, params)` + single pack compiler = pack-service. |
| 1.4 | `semantic_content_pipeline.py:656-719` `_quality_gate` + `manifest_validator:24-52` + `validation/*` (pack/retrieval/glossary/educational) + `evaluation/quality_scoring:9-30` + `educational/concept_coverage_validator` | 4 overlapping gates contradictory thresholds (`duplicate_ratio>=0.10` vs `coverage_score len/content` `1.0` when `content=0` vs `glossary=[]` passes but `semantic:690` requires `definition_coverage>=90%`). `manifest_builder:30` `retrieval_index_version=v2` vs `semantic:563` `semantic-v1`. | Single `manifest_validator` + `retrieval_validator` with unified thresholds. |
| 1.5 | `semantic_content_pipeline:721-745` `_clean_text`/`is_header_footer`/`is_toc`/`_classify_and_clean` vs `educational/toc_cleanup:17-69` vs `educational/chunk_normalizer:20-70` | 3 TOC/clean layers, double `normalize` (`262` then `268`). | One `TocCleanup` → `ChunkNormalizer`. |

---

## 2. Bugs — Fallback Chain (Wrong-Content Packs)

`pack_generator.py:216-308` `_search_chunks_by_metadata`:

* Exact → `PACK_FALLBACK_TRIGGERED:252` if 0 → iterate `_fallback_attempts:343-402`:
  * `remove_language_filter:383` (kn→en silently),
  * `normalized_subject_filter:386` (`maths→mathematics` via `SUBJECT_QUERY_ALIASES`),
  * `chapter_fuzzy_match:392` → `_scroll_chunks_with_fuzzy_chapter:508` `SequenceMatcher>=0.82` with `limit=max(limit,50000):273` → **50k points RAM**, substring fuzzy can match wrong chapter.
* `PACK_FALLBACK_SUCCESS:284` returns without annotating which fallback — caller publishes as if exact, no language-mismatch gate.
* `_build_qdrant_filter:423-428` `language_filter_values` path vs `_normalize_metadata_value:314` inconsistent `en` vs `english` → `MatchAny` not triggered.
* Mirror bug in `content-pipeline/main.py:893-935` dense/lexical fallback to `None` filter — undocumented degraded unfiltered results.

**Fix:** Annotate fallback (`used_fallback: reason`) and fail `quality_gate` if `language` fallback used for `kn` pack.

---

## 3. Bugs — Hardcoded Paths / Validation / Error Swallow

| ID | File:Line | Severity | Bug |
|---|---|---|---|
| 2.2 | `app/main.py:32-36` `LOCAL_RUN_BASE.parents[2]/.local_run` + `:210-211` `"/shared/generated_pack"` | HIGH | Breaks in Docker (`/shared` expected, `.env.example:18-51` docs `/shared`). |
| 2.3 | `pack_generator:305-308` `except Exception: return []` | HIGH | Swallows `ConnectionError/AuthError`, downstream raises misleading `PackGenerationNoContentError` vs 500. Also `content-pipeline/main.py:305` same. |
| 2.4 | `pack_storage/pack_repository:125-126` `except (OSError,JSONDecodeError): pass` + `:68-78` `remove_pack` no try | MEDIUM | Corrupt `manifest.json` silently stale; partial `rmtree`/`unlink` leaves orphan registry. |
| 2.5 | `pack_system/manifest_validator:10-16` requires `grade/subject/chapter` non-null but `manifest_builder:31-36` allows `None` → language packs `lang_kn_5` always invalid. Also `language` stored `"english"` not `"en"` via `normalize_curriculum_name` vs code expects `"en"`. | HIGH | Contradiction — lang packs never validate. |
| 2.3e | `app/main.py:79-103` `try: from shared.pack_schemas import ... except: class PackGenerationRequest...` bare except | MEDIUM | Silent schema divergence if `shared` missing. |
| 2.4b | `pack_generator:542-621` `debug_pack_query()` `print` hardcoded `grade=5 maths animal jumps` in prod. | LOW | Remove. |

---

## 4. Bugs — Schema Mismatches

* `pack_repository:38-49` `checksum/content_checksum` sha256 but `_infer_artifact_counts:218-248` vs `semantic_content_pipeline:405-475` `total_chunks/rag_chunks/average_chunk_length` not in `artifact_counts`.
* `manifest_builder:12` `retrieval_index_version=v2` vs `semantic:563` `semantic-v1` never caught (`retrieval_validator:9-11` checks manifest key only).
* `api/pack_response_models:48-61` `ManifestResponse` vs `PackListItem:64-80` — list hydrates `archive_path/compressed_size_mb` but manifest endpoint lacks them — client must join two calls.
* `api/pack_response_models:9-13` `PackSummaryItem` requires `chunk_ids` but `semantic:955` summaries lack `chunk_ids`; `evaluation/quality_scoring:17` `coverage_score = (...) / max(1,content*5)` returns 1.0 for `content=0`.
* `pack_routes:84` `search_packs?version=1.0` never matches `1.0.0` SemVer; `sync/delta` `compressed_size_mb` 0.0 when archive missing.

---

## 5. Duplicated Retrieval vs Content-Pipeline

| Concern | `pack-service` | `content-pipeline` | Gap |
|---|---|---|---|
| Vector client | `QdrantClient` + `_scroll_points:446` `with_vectors=True` (loads vectors then discards) | `make_qdrant_client` + `scroll(with_vectors=False)` | Same collection `educational_chunks_bge_m3`, two loops; memory waste. |
| Filter | `_build_qdrant_filter:404` diverged `language_filter_values` | `shared/vector_store.build_filter` | Drift. |
| Hybrid | Missing — only `_validate_rag:609` hardcoded queries `What is photosynthesis?` | `HybridChunkStore` BM25 + `EducationalRetrievalEngine` 0.35/0.35/0.30 | Pi needs `sqlite_vec`/`hybrid_chunk_index.json` not packaged by pack-service. |
| Graph | `curriculum_graph_path` stored but never used | `CurriculumGraph` + `ConceptIndex` used in `_retrieve_candidate_hits:943` | Two stores unsynced. |
| Quantize | `_quantize_embedding:756` 8-bit ad-hoc | `SimpleEmbeddingModel` hash + `SentenceTransformer` BGE-M3 | Two paths, no shared quantizer. |

---

## 6. Dead Code

`_generate_glossary:627`, `_compress_pack:738`, `_compute_checksum:770`, `debug_pack_query:542`, `semantic_content_pipeline:889-1028` heuristic fallbacks, `benchmark_pack:161` ignores `pack_id` (runs all packs), `preview_routes:26-63` duplicate of `pack_routes:106-147`, `DeltaBuilder:1-13` trivial wrapper, `coverage_analyzer` vs 2 other coverage validators.

---

**Priority:** Unify filter → `shared`, annotate fallback (fail lang pack if `en` used for `kn`), fix `500` vs `NoContent` error, fix manifest language `"en"` vs `"english"` + `lang` pack validation, loop 8× duplicate `preview_routes`, dedupe `QdrantClient` + remove `with_vectors=True`.

*Evidence: 788-line `pack_generator`, `LOCAL_RUN_BASE/.local_run` vs `/shared` drift, live `GET /packs` 200.*
