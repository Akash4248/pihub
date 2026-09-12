# Content-Pipeline Service Audit — 2026-09-12

**Auditor:** Muse Spark (OpenCode)  
**Scope:** `pihub/backend/content-pipeline/` + `pihub/backend/pack-service/` + `shared/` (vector/curriculum)  
**Declared goal:** *Generate artifacts (quiz, flashcard, glossary, summary, chapter-notes, …) from textbooks AND create Qdrant chunks for on-device LLM context; `pack` service should only serve packs, remaining lightweight on Pi.*

---

## 1) Executive Summary

The `content-pipeline` today is **three systems in one** — a decent textbook ingestor/chunker, an over-engineered multi-agent artifact factory, and a duplicated retrieval/pack stack that collides with `pack-service`. That collision is the root cause of the duplication you suspected.

**Verdict: 5.8 / 10 — functional but needs consolidation before Pi deployment.**

- What is **good**: `EducationalChunkerV2` + `extraction_cleaner` + `formula_preserver` is a genuine improvement over naive fixed-size splitting; hybrid BM25+dense retrieval works; metadata normalization is thorough.
- What is **bad**: artifact generation is expensive, sequential, LLM-dependent and has weak quality gates; pack compilation exists in *two* places; the RAG stack is instantiated twice; inference path adds 2–3x latency.
- What **must change**: decide *single* pack authority (pack-service), reduce content-pipeline to **ingest → clean → chunk → embed → evaluate** and delete duplicated pack/auth paths; slim Pi image from ~3 GB to < 1 GB.

---

## 2) Declared Goal vs Actual Implementation

| Declared goal | Where it lives | Actual status |
|---|---|---|
| **Textbook → chunks for Qdrant / on-device** | `app/textbook_ingest.py:412` (`extract_text_from_pdf`), `app/content_pipeline/educational_chunker.py:25`, `app/main.py:580` (`_store_chunks`) | **Done, but bloated.** Docling → PyMuPDF → Tesseract cascade is correct; `EducationalChunkerV2` adds noise stripping, sentence-aware splitting, overlap, formula atomicity (`app/content_pipeline/formula_preserver.py:1`, `concept_boundary_detector.py:1`). Metadata enrichment via `TextbookMetadataExtractor` + `CurriculumGraph` is solid. Latency penalty is real (torch + sentence-transformers + docling in one container). |
| **Quizzes / flashcards / glossary / summaries** | `app/educational_intelligence/*` (quiz `quiz_generator.py:12`, glossary `glossary_extractor.py:47`, summary `summary_generator.py:11`, flashcards, chapter_notes, etc.), `app/main.py:635` (`_generate_artifacts`) and `app/educational_intelligence/agentic_orchestrator.py:347` (`ArtifactAgent`) + `enriched_content_agent.py:65` (`EnrichedContentAgent`) | **Implemented 3×.** Simple generators (`Pipeline._generate_artifacts`), agentic loop (`ArtifactAgent` = analyze → plan → critique → improve, up to `MAX_ITERATIONS=3`), and enriched agent (+ web videos/simulations/media). All ultimately proxy to `inference-service` via `InferenceClient` (`inference_client.py:36` `_post`). No caching; each chapter triggers 8 LLM calls + N critique calls. |
| **Qdrant context for on-device LLM** | `app/main.py:183` `Pipeline` (Qdrant + `HybridChunkStore` + `sqlite_vec_exporter.py:1`), `shared/vector_store.py:1`, `shared/config.py:36` `qdrant_collection=educational_chunks_bge_m3` | **Duplicated.** Qdrant is written by content-pipeline (`_store_chunks:605` `upsert_chunks`) *and* read by pack-service (`pack_generator.py:446` `_scroll_points`). Hybrid store writes a parallel JSON snapshot `hybrid_chunk_index.json` + optional `sqlite_vec` export for Pi. Two vector stores, two normalizers, same BGE-M3 embedding (384/1024 dims). |
| **Pack service = just serve packs, lightweight on Pi** | `pihub/backend/pack-service/app/pack_generator.py:59` (`PackGenerator`), `pack_storage/pack_repository.py:1`, `api/pack_routes.py:66` (`/packs/list|/download|/manifest`) vs `content-pipeline/app/main.py:1171` (`build_pack_preview`, `build_learning_pack_preview`) and `app/educational_intelligence/pack_compiler.py:10` (`PackCompiler`) | **Inverted.** content-pipeline *compiles* packs (ZIP + `content.json`, `summaries.json`, `glossary.json`, `quizzes.json`, `flashcards.json`, `enrichment.json`, `metadata.json` — `pack_compiler.py:38`), while pack-service *also* compiles packs via `SemanticContentPipeline` (`pack_generator.py:686`). Pi was supposed to be light but inherits `content-pipeline/Dockerfile:10` deps (poppler + tesseract + torch + transformers + docling + ffmpeg ≈ 2.8 GB uncompressed). |

---

## 3) How It Actually Works (End-to-End)

```
PDF (upload or /shared/content/class_7/...)
  │
  ├─► StructuredTextbookIngest.extract_text_from_pdf()  [textbook_ingest.py:412]
  │     Docling.export_to_markdown() → fallback fitz.get_text() → fallback pytesseract OCR (2× pixmap)
  │     + extract_images() [textbook_ingest.py:456] — persists PNGs >200×200, heuristic caption link
  │
  ├─► TextbookMetadataExtractor.extract_from_path() [textbook_ingest.py:63]
  │     grade/subject/language from path tokens + regex (class_6, grade_8, etc.)
  │     merged with language detection from text [textbook_ingest.py:178]
  │
  ├─► EducationalChunkerV2.chunk_educational() [content_pipeline/educational_chunker.py:98]
  │     clean_raw_text() [extraction_cleaner.py:85] → SectionParser → ParagraphMerger
  │     → _is_noise / _split_long_paragraph / FormulaPreserver / ConceptBoundaryDetector
  │     → ChunkMetadataBuilder → repair_chunks() (dedupe, header/footer, OCR noise) [extraction_cleaner.py:174]
  │     + _enrich_chunks() [main.py:546] (topic/concept inference via CurriculumGraph)
  │
  ├─► Pipeline._store_chunks() [main.py:580]
  │     prefix = grade+subject+chapter+section+topics+concepts  →  SentenceTransformer (BAAI/bge-m3) encode
  │     → Qdrant upsert + HybridChunkStore.upsert() + CurriculumGraph.build_from_chunks()
  │     → ConceptIndex rebuild + GraphBuilder.build() (topic_relations / concept_links / prerequisites)
  │
  ├─► Artifact generation (two parallel paths)
  │     Path A: Pipeline._generate_artifacts() [main.py:635] → Summary/Glossary/Quiz/Flashcard/ChapterNotes/...
  │             each calls InferenceClient → inference-service /ai/content/{summary,glossary,quiz,...}
  │             fallback to heuristic (e.g. _heuristic_generate [quiz_generator.py:67])
  │     Path B: ArtifactAgent.generate_artifacts() [agentic_orchestrator.py:446]
  │             ContentAnalyzer → GenerationPlanner → per-task LLM + ArtifactCritiquer loop (score ≥7.0)
  │     Path C: EnrichedContentAgent.generate() [enriched_content_agent.py:116]
  │             = Path B + WebResourceFinder (YouTube/PhET/OLabs/GeoGebra) + MediaStore (Cobalt download + ffmpeg compress)
  │             + EnrichmentStore (Qdrant enrichment_resources collection) + Mongo media metadata
  │
  ├─► PackCompiler.compile() [educational_intelligence/pack_compiler.py:18]
  │     writes 10 JSON files + ZIP under /tmp/pihub_packs/<slug>/
  │
  └─► Retrieval: Pipeline._search() [main.py:988]
        Hybrid: Qdrant dense (build_filter) + HybridChunkStore.lexical BM25 + concept_index routing
        → CurriculumRouter expansion → EducationalRetrievalEngine.rank() [retrieval_engine/educational_retrieval_engine.py:101]
        → final_score = 0.35*semantic + 0.35*lexical + 0.30*educational (topic/subject/chapter/chunk_type)

Pack-service (separate container, same Qdrant):
  PackGenerator._search_chunks_by_metadata() [pack-service/app/pack_generator.py:216]
  → _scroll_chunks_by_metadata / _scroll_chunks_with_fuzzy_chapter + fallback chain
  → SemanticContentPipeline.build() → quality gate → PackRepository.save_pack() → tar.gz + manifest.json
  → served via /packs/{id}/manifest|preview|download|validate|benchmark [pack-service/app/api/pack_routes.py:66]
```

Dead/orphan paths found (not wired or contradicting the above):
- `app/sync_and_import_all_packs.py:1` — uses hardcoded `/home/akash/Documents/content_packs/installed`, synthetic fallback quizzes, private SQLite schema; never called.
- `app/concept_graph_exporter.py:1` — standalone `concept_nodes` / `concept_prerequisites` SQLite exporter; not referenced by pipeline.
- `app/generated_pack_ingestor.py:48` — ingests offline `generated_pack` dirs (`/shared/packs/grade_0/mixed/generated_pack`); valid but undocumented and bypasses quality gates.

---

## 4) Ratings

| Dimension | Score (0–10) | Rationale |
|---|---|---|
| **Goal alignment** | 5 | Does all three declared things, but pack responsibility is split and Pi-light requirement is violated (heavy image runs heavy work). |
| **Chunking / RAG accuracy** | 7.5 | Best part of the repo. `EducationalChunkerV2` + `extraction_cleaner` is thoughtful; 180–1100 char target + overlap + repair metrics. Hybrid BM25 (0.55 dense + 0.35 lexical + 0.10 filter boost, `hybrid_chunk_store.py:236`) + reranker is above average. Deduction: embedding prefix can dominate semantics; dedup key is shallow (`hybrid_chunk_store.py:337` sha256 of first 4000 chars). |
| **Artifact quality** | 4.5 | LLM path depends entirely on `inference-service` availability; fallback heuristics produce generic distractors (`quiz_generator.py:140` “An unrelated idea…”) and empty definitions (`glossary_extractor.py:157` “Key concept related to…”). No JSON schema validation after LLM; `_parse_artifact` [agentic_orchestrator.py:675] does `find("{"…)` slicing. Critique loop asks the *same* LLM to grade itself — accuracy self-report is inflated. |
| **Latency** | 3.5 | Cold start loads BGE-M3 on CPU twice (Pipeline + HybridChunkStore) + Docling model; a single chapter `_generate_artifacts` fans out to 8 async LLM calls and up to 24 with critique; `_encode` is sync. Pi will not meet “proper low latency”. |
| **Reliability / correctness** | 4 | Silent fallbacks everywhere (`main.py:304` `ensure_ready` swallowed, `SimpleEmbeddingModel` hashlib fallback silently replaces semantic search with hash). `import fitz` inside try, `pytesseract` optional → silent quality loss. `GeneratedPackIngestor` hardcodes `/shared/packs` without config. |
| **Pi / offline suitability** | 3 | Dockerfile pulls `build-essential + poppler + tesseract + ffmpeg + torch + transformers + docling` (~3 GB). Qdrant is required at runtime (`QdrantClient(url=qdrant_url)`). Correct Pi pattern is SQLite-vec/LanceDB + pre-exported snapshot; the exporter exists (`sqlite_vec_exporter.py:81`) but is never the *default* path. |
| **Maintainability** | 4 | ~1700 LOC in `main.py`, three artifact agents, two pack compilers, duplicated normalization (`shared/text_normalization.py` vs local `_normalize_subject`). Strict typing is good; god-class `Pipeline` is not. |
| **Security / integrity** | 6 | Path traversal guarded by `_resolve_content_path` [main.py:462]; filename sanitization present; pack checksum computed but HMAC sync lives elsewhere (pihub/distribution). |
| **Overall** | **5.8** | Strong chunking core, weak productization. Priority is to shrink and deduplicate before Pi hardening. |

---

## 5) Duplication of Work — Concrete Findings

| Duplication | Location A | Location B | Impact |
|---|---|---|---|
| **Pack compilation** | `content-pipeline/app/educational_intelligence/pack_compiler.py:18` (ZIP + 10 JSONs) | `pack-service/app/pack_generator.py:662` `_create_pack` + `SemanticContentPipeline` | Two manifests, two version schemes (`2.0.0` vs `0.1.0` in `main.py:1186`), two quality gates. Pack-service already has validator, scorer, benchmark (`api/pack_routes.py:138`, `158`, `161`). **Fix: delete PackCompiler from content-pipeline; pipeline emits only `artifacts + chunks` and calls pack-service /POST.** |
| **Vector store** | `content-pipeline/app/main.py:187` `make_qdrant_client` + `_store_chunks` | `pack-service/app/pack_generator.py:75` `QdrantClient` + `_scroll_points` | Same collection, two clients, two filter builders (`shared/vector_store.build_filter` vs `pack_generator._build_qdrant_filter`). Divergence already visible (subject aliases handled differently). **Fix: single vector-store client in `shared/`.** |
| **Hybrid / lexical index** | `HybridChunkStore.search` [retrieval_engine/hybrid_chunk_store.py:122] + `EducationalRetrievalEngine.rank` [educational_retrieval_engine.py:101] | `pack-service` has no lexical fallback; but `HybridChunkStore.export_sqlite_vec` *is* the Pi offline path that pack-service should consume | Hybrid snapshot is built in content-pipeline but pack-service packs still scroll Qdrant live — so offline packs contain Qdrant vectors the Pi cannot query. **Fix: pack-service should package the Hybrid snapshot / sqlite_vec DB, not live Qdrant points.** |
| **Curriculum graph** | `CurriculumGraph` (`shared/curriculum_graph.py`) + `ConceptIndex` (`curriculum_graph/concept_index.py`) + `GraphBuilder` trio | pack-service imports same graph for pack manifest metadata (indirect) | Built twice on startup, saved to two JSONs (`curriculum_graph.json` + `curriculum_relation_graph.json`). Keep one builder behind a shared service. |
| **Artifact generators** | Simple generators per `main.py:635` + `agentic_orchestrator.ArtifactAgent` + `enriched_content_agent.EnrichedContentAgent` | `inference-service/app/context/*` (assumed) | Three orchestrators for the same 8 artifact types. `EnrichedContentAgent` already composes `ArtifactAgent` — the simple path should be removed or made an alias. |
| **Normalization** | `shared/text_normalization.py` | `_normalize_subject` in `generated_pack_ingestor.py:22`, `pack_generator.py:310`, `textbook_ingest.py:43` | Inconsistent canonical subjects (`maths` vs `mathematics` vs `maths_extended`). Centralize in `shared/`. |
| **Preview / debug endpoints** | `/debug/pack-preview` + `/debug/learning-pack-preview` + `/build/pack` [main.py:1432, 1497] | `/packs/{id}/preview` + `/packs/{id}/manifest` [pack-service] | Same JSON served from two services; clients cannot know which is authoritative. |

**Estimated duplication cost:** ~30–40% of content-pipeline LOC exists solely to replicate pack-service, and pack-service replicates ~25% of the retrieval logic.

---

## 6) Pi / Lower-Work Assessment

Your instinct is correct: **content-pipeline must NOT run heavily on Pi.**

What is heavy today:
- `torch` (CPU) + `sentence-transformers` + `BAAI/bge-m3` (~2 GB model cache under `/shared/models/...` [shared/config.py:42]), Docling (layout model), Tesseract, FFmpeg for `MediaStore`.
- Per-PDF OCR at 2× pixmap if `fitz.get_text()` < 40 chars [textbook_ingest.py:435].
- Hybrid store rebuilds postings on every `upsert` + writes JSON snapshot synchronously [hybrid_chunk_store.py:119, `save_snapshot`].

Recommended Pi topology (already half-present in your repo):

```
Cloud / Build machine          Pi (offline)
─────────────────────          ────────────
content-pipeline:              pihub (light)
  chunk + embed + artifacts      ├─ sqlite-vec / LanceDB (from HybridChunkStore.export_sqlite_vec [hybrid_chunk_store.py:81])
  → pack-service packages        ├─ packs/*.tar.gz (pre-validated, with retrieval_index/index.json)
  → media compressed (ffmpeg)    ├─ /shared/media (already-compressed, CRF 28 [shared/config.py:108])
  → push to /shared/packs        └─ NO torch, NO docling, NO tesseract, NO qdrant
                                 pihub serves RAG from sqlite-vec; sync is HMAC-verified tar.gz
```

The code already anticipates this (`media_max_total_gb`, `cobalt_api_url`, `sqlite_vec_exporter`), but startup still `ensure_collection` against Qdrant and fails open without exporting. Flip the default: **build once in cloud, export sqlite_vec + tar.gz, Pi only serves.**

---

## 7) Out-of-the-Box Tools — Replace vs Keep

> Criterion you asked: *high accuracy, low latency, proper working, minimal Pi work.*

### A) Keep but Trim

| Current | Verdict | Note |
|---|---|---|
| `EducationalChunkerV2` + `extraction_cleaner` | **Keep, promote** | Better than generic splitters for NCERT (formula/table/noise handling). Consider open-sourcing as a library. |
| `BAAI/bge-m3` | **Keep (cloud)**, not on Pi | 1024-dim multilingual is correct for en/kn/hi. On Pi, switch to quantized `bge-m3` or `paraphrase-multilingual-MiniLM-L12-v2` (384 dims, 5× smaller) already used as `SimpleEmbeddingModel` fallback — make that the Pi default. |
| Hybrid BM25+dense + `EducationalRetrievalEngine` | **Keep logic, drop duplication** | Fusion weights are sane; merge with HybridChunkStore into a single `shared/retrieval` package. |

### B) Out-of-the-Box Replacements (with fit for your use)

| Need | High-accuracy, low-latency options | Fit for PiHub | Suggested action |
|---|---|---|---|
| **PDF → text + structure** | **Docling** (you already use), **Marker** (via `marker-pdf` / `surya`), **Unstructured (`unstructured[pdf]`)**, **GROBID** (academic), **PyMuPDF** (fast, no layout) | Docling is SOTA for layout+reading-order; Marker is faster on Pi but server-side only; GROBID overkill for NCERT | Keep Docling for accurate ingestion (cloud only). On Pi, never parse PDFs — ship pre-chunked packs. Remove `pytesseract`/`poppler` from Pi image. |
| **Chunking** | **Chonkie** (`chonkie.SemanticChunker`, `LateChunker`), **LlamaIndex `SemanticSplitterNodeParser`**, **LangChain `RecursiveCharacterTextSplitter` + `SemanticChunker`**, **RAGatouille** | Chonkie is the current SOTA for semantic chunking (sentence-transformers + late chunking), 2–3× faster than your custom V2 on CPU | Benchmark `Chonkie[late]` against `EducationalChunkerV2` on 20 NCERT PDFs; keep whichever wins on recall@5 with your retriever (expect tie — then keep yours for formula awareness). |
| **Embeddings / rerank** | **BGE-M3**, **Jina v3**, **Nomic Embed**, **Snowflake Arctic**, **Cohere Rerank 3**, **BGE Reranker** | Run embeddings cloud-side; ship quantized int8 vectors in packs | Already on BGE-M3 — keep. Add optional reranker (`bge-reranker-base`) as post-`EducationalRetrievalEngine` step for +3–5 pts recall. |
| **Artifact generation (quiz/flashcard/glossary/summary)** | **Questgen AI** (`questgen`), **LlamaIndex `QuestionGenerator`**, **Haystack `QAPipeline`**, **GraphRAG** for chapter-notes, **Instructor + Pydantic** for JSON-guaranteed LLM calls | Your generators do no JSON schema enforcement; open tools do. Questgen has MCQ distractors out-of-the-box with Bloom taxonomy | Replace heuristic fallbacks with Questgen for quiz + Instructor+JSON schema for all LLM calls. Keep your prompts but validate with `pydantic`/`jsonschema` + retry, eliminating `_parse_artifact` slicing failures. |
| **On-device vector DB (Pi)** | **`sqlite-vec`** (you already have `sqlite_vec_exporter.py`), **LanceDB**, **Chroma (PersistentClient)**, **Qdrant (embedded Rust)** | `sqlite-vec` is smallest (single `.db`), LanceDB has best hybrid BM25+ANN | Ship sqlite-vec DB produced by `HybridChunkStore.export_sqlite_vec()` as the pack's `retrieval_index/index.json` replacement. Drop Qdrant from Pi compose (`docker-compose.pi.yml` should not start `qdrant`). |
| **Pack serving / sync** | Pack-service already does this; alternatives are **MinIO presigned URLs** or **PouchDB replication** | Your HMAC-verified `pihub/distribution/pack_manager.py` is correct | Keep pack-service as sole pack authority. Content-pipeline should push artifacts to it (POST) instead of building tar.gz itself. |
| **Media (videos/simulations)** | **Cobalt API** (already in config `COBALT_API_URL`), **yt-dlp**, **PhET catalog** | You have Cobalt + ffmpeg CRF 28 pipeline; good | Keep; enforce `media_max_total_gb` [shared/config.py:122] and `media_max_videos_per_topic` [shared/config.py:110] — currently not enforced after `MediaStore.store_video`. |

**Concrete next tool to try this week:**

1. Replace `PackCompiler` path with `pack-service` call — deletes ~85 LOC + removes ZIP divergence immediately.
2. Swap `SimpleEmbeddingModel` hash fallback for a real 384-dim MiniLM on Pi — 1 line config change, 10× better recall than hash buckets (`main.py:82` `blake2b % dimension`).
3. Wrap every `InferenceClient._post` with `instructor` + Pydantic model for `QuizItem`, `GlossaryEntry`, etc. — moves `generated_by: heuristic` from common path to exception path.

---

## 8) Recommendations (Ordered)

1. **Single pack authority.** Delete `app/educational_intelligence/pack_compiler.py` usage from `main.py:1171,1252` and `app/main.py:1514` (`pack_compiler.compile`); route through `pack-service` API. Mark `app/sync_and_import_all_packs.py` and `app/concept_graph_exporter.py` deprecated or delete.
2. **Slim Pi image.** Create `Dockerfile.pi` without `docling`, `torch`, `transformers`, `tesseract`, `poppler`, `ffmpeg` build deps. Pi image only contains `fastapi + uvicorn + sqlite-vec + FastEmbed`. Target < 800 MB.
3. **Merge retrieval.** Move `HybridChunkStore` + `EducationalRetrievalEngine` + `shared/vector_store` into `shared/retrieval/`; both services import it. Fix Pi default to `HybridChunkStore.export_sqlite_vec` not Qdrant.
4. **Consolidate orchestrators.** Keep `EnrichedContentAgent` as the single entry (`/build/enriched-artifacts`), make `/build/agentic-artifacts` a deprecated alias, delete `Pipeline._generate_artifacts` inline generators or delegate to the agent.
5. **Add JSON schema gating.** Every LLM output validated; failed parse → retry once → fallback. Track `quality_gate.passed` [pack-service/app/pack_generator.py:688] in observability.
6. **Instrument latency.** Add timings for `extract → chunk → embed → store → artifact` per chapter; expose at `/health` alongside existing checks [main.py:1294]. Alert if chapter artifact > 90 s.
7. **Normalize once.** Remove `_normalize_subject` duplicates; import from `shared/text_normalization`.

---

## 9) Full-Service Replacement — Can One OSS Tool Replace Everything?

**Question:** is there a single open-source tool that does *all* of: PDF textbook ingestion → curriculum-aware chunking (+ formula/table preservation) → BGE-M3 embeddings → Qdrant *and* sqlite-vec hybrid → 8 artifact types (quiz, flashcard, glossary, summary, chapter_notes, learning_objectives, misconceptions, applications) → enrichment (YouTube + PhET/simulations + multilingual en/kn) → offline packs for Pi — with high accuracy + low latency + proven?

**Answer: No. Not as a drop-in.** Every OSS candidate covers *one half* of your pipeline. The search (Sep 2026) across GitHub/PyPI/arXiv confirms the split is structural: RAG pipelines don't do educational artifacts; educational generators don't do RAG/offline packs. Evidence below; then what *does* come closest and what composition would actually replace the service.

### 9.1 Candidates Tested (internet / GitHub, Sep 12 2026)

| Candidate | Stars / maturity | What it does | Coverage of your 4 requirements | Gap vs you |
|---|---|---|---|---|
| **PromtEngineer/localGPT** (22.2k★) | `localGPT` — private RAG platform, Docling parsing, LanceDB + hybrid search (dense + FTS RRF) + cross-encoder reranker, Ollama, late chunking, verified eval harness | **RAG half only.** Excellent ingestion: Docling + OCR fallback + contextual enrichment + per-document overviews + LanceDB hybrid. Single SQLite file = whole index (`cp` backup). Measured defaults. | ✅ PDF→chunks ✅ embeddings ✅ hybrid RAG (better than yours on reranking) ❌ *no* quiz/flashcard/glossary/summary generation ❌ *no* curriculum graph/prerequisite map ❌ *no* pack compiler for Pi | Would replace `textbook_ingest` + `HybridChunkStore` + `EducationalRetrievalEngine` with a *better* implementation, but you'd still need to bolt artifact generation on top. Latency: similar (torch + reranker). |
| **thendralmagudapathi/RAG-for-NCERT** | NCERT-specific RAG, LangChain + PyMuPDF + Qdrant + Ollama | Almost 1:1 with your *retrieval* half for NCERT 11/12; recursive loader per subject folder. | ✅ NCERT PDF→Qdrant RAG ✅ Qdrant+LangChain ❌ no artifacts ❌ no chunk repair/formula preservation ❌ no packs | Proof that NCERT+Qdrant pattern is common, but not a full replacement. |
| **shakirsadiq6/open-rag-pipeline** | Multi-format ingestion, configurable embeddings (OpenAI/Cohere/local), Qdrant/Milvus/Chroma | Generic RAG ingestion + CLI (`open-rag ingest/query`). | ✅ ingest + embed + multi-vector-store ❌ no NCERT metadata ❌ no artifacts ❌ no sqlite-vec Pi export | Plumbing, not product. |
| **giuseppeferretti/sqlite-rag-mcp** + **sqliteai/sqlite-rag** | 100% offline RAG, SQLite + sqlite-vec + FTS5, hybrid RRF, Ollama `nomic-embed-text`, MCP server | **Exactly your Pi requirement** — one `.db` file is the index; ideal for Pi offline. | ✅ sqlite-vec + FTS5 hybrid + RRF (same fusion you wrote by hand) ✅ offline ✅ tiny ❌ no PDF extraction ❌ no artifacts ❌ no packs | Perfect *Pi reader*; needs a *cloud builder*. |
| **deepset-ai/haystack** (26.5k★) | Framework, not app: `Pipeline` + `Agent`, modular components, async, model-agnostic, production hardening | You build the pipeline declaratively; integrations for Docling, Qdrant, LanceDB, etc. exist. | ✅ framework can express your whole pipeline ✅ eval + tracing built-in ❌ *no* NCERT artifact templates out of box ❌ you still write the 8 generators | Replaces *architecture* (deletes your god-class `Pipeline` 1700 LOC), not the educational logic. |
| **docling-project/docling** (66.3k★) | MIT, IBM, LF AI & Data — PDF/DOCX/PPTX/EPUB/HTML → `DoclingDocument` + Markdown/JSON/HTML export; `DoclingLoader` for LangChain | Best OSS PDF understanding (layout, reading order, tables, formulas, OCR, ASR). | ✅ extraction (better than your `fitz`+`pytesseract` cascade) ❌ no chunking strategy ❌ no embeddings ❌ no artifacts | Keep it (you already do) — but only the *first mile*. |
| **Educational-artifact only** (5 repos) | `fbellame/pdf-to-quizz` (54★, Streamlit+LangChain, 2 Q/page), `h6y3/quizcraft` (Claude, OCR), `quizgen-ai` (PyPI, Anki), `FranRguezCer/flashcards-anki-gen` (LangGraph 5-node + quality gate), `skojaku/llm-quiz` | PDF → quiz/flashcard/glossary only. `quizgen-ai` does Anki export; `flashcards-anki-gen` has LangGraph agentic loop like your `ArtifactAgent`; `pdf-to-quizz` is closest single-file demo. | ✅ artifacts (1–2 types) ❌ no chunking/RAG/Qdrant/packs/multilingual | Would replace 1 generator, not the service. |
| **L2Q — Self-hosted Lecture-to-Quiz** (arXiv 2603.08729v1) | Local LLM + deterministic QC gates, 5-stage pipeline (PDF ingestion→topic plan→MCQ drafting→QC→export JSONL/CSV), API-free | Only full *artifact* pipeline with QC trace — 24 curated MCQs from 15 runs, `Qc acceptance 1.0` with retry overhead. | ✅ local LLM (no external API) ✅ MCQ QC gates ✅ static export ❌ only MCQ ❌ no RAG DB ❌ no packs/simulations | Inspiration for your QC layer, not a service replacement. |

No row scores full marks on all four of your requirements (ingest→RAG, artifacts×8, Qdrant+sqlite-vec, packs+Pi).

### 9.2 Why No Single Tool Exists (structural)

1. **Educational artifacts are curriculum-constrained.** Generic RAG tools (Haystack, LocalGPT, open-rag-pipeline) assume open-domain QA; they don't enforce NCERT grade/subject/chapter metadata, prerequisite maps, or Bloom-taxonomy distractors. Your `TextbookMetadataExtractor` + `CurriculumGraph` + `ConceptIndex` trio has no OSS equivalent — the nearest is `RAG-for-NCERT` which hardcodes two grades and no graph.

2. **Artifact diversity vs RAG purity.** LocalGPT evaluated late-chunking, verification, cross-ref hops and *disabled* them for being noise — your 8 artifact types (including `misconceptions`, `applications`, bilingual videos) are deliberately out-of-scope for a *retrieval* platform. Mixing them into one service is why your artifact quality gates feel bolted on (`_parse_artifact` slicing).

3. **Offline-pack distribution is your invention.** No OSS RAG ships HMAC-verified, delta-syncable, versioned `.tar.gz` packs with `retrieval_index/index.json` + `enrichment.json` + `media/` — that's your curriculum-builder/pack-service/pihub sync layer. Replacing it means replacing your distribution contract.

4. **Multilingual NCERT + Pi constraint** eliminates most OSS assumptions (English, cloud, Qdrant-always-on). `sqlite-vec` candidates solve Pi but only as a *reader*; something must still *build* the packs in cloud.

### 9.3 Closest Drop-In Compositions (if you insist on replacing the service)

If "replace entire service" is the hard requirement, these *compositions* are the minimum that reach ~90% coverage with fewer custom lines than you carry today:

**Option A — Minimal change, highest accuracy (recommended): `LocalGPT` + `quizgen-ai/Questgen`**
- Keep LocalGPT as ingestion+RAG (Docling→LanceDB hybrid→reranker); it outperforms your custom `HybridChunkStore` + `EducationalRetrievalEngine` on measured faithfulness and has the eval harness to prove it.
- Add `quizgen-ai` or `h6y3/quizcraft` as artifact sidecar for quiz/flashcard/glossary; keep your 5 remaining generators behind `Instructor` JSON schema.
- Still need a thin pack compiler to emit `sqlite-vec` from LanceDB and wrap `.tar.gz`. Effort: ~400 LOC new vs ~1700 LOC deleted from `main.py`.
- Trade-off: LanceDB not Qdrant, but Qdrant↔LanceDB migration is trivial; LocalGPT already abstracts it.

**Option B — Framework rebuild: `Haystack 3` + `Docling` + `open-rag-pipeline` patterns**
- Express each current stage as a Haystack `Pipeline` component (`DoclingConverter → Chonkie Chunker → BGE-M3 Embedder → QdrantWriter` + parallel `ArtifactGenerator` using `Instructor`); gains async, streaming, telemetry, and the 26k★ maintenance surface.
- Repackage packs via Haystack's `DocumentStore` export + your existing `PackRepository`.
- Effort: ~600 LOC; you delete the god-class `Pipeline` and unify the two vector clients.
- Trade-off: Framework overhead, but you inherit Haystack's tested hybrid + reranking recipe (`pipeline/chunker.py` + `embedder.py` + `retriever.py` in `cerenaaa/rag-pipeline` is a concrete reference with RAGAS eval).

**Option C — Pi-first offline: `sqlite-rag-mcp` + `RAG-for-NCERT` loader**
- Cloud builder = `RAG-for-NCERT` ingestion (NCERT-aware) → export `sqlite-rag-mcp` `.db` (sqlite-vec+FTS5+RRF). Pi only runs the MCP reader — zero vector DB to operate.
- Artifacts generated separately by `flashcards-anki-gen` (LangGraph quality gate is closest to your `ArtifactAgent` critique loop, but better: capped retry + state graph).
- Effort: smallest Pi footprint, largest artifact gap to fill.
- Trade-off: sqlite-vec reader is perfect for latency (no Qdrant), but you lose Qdrant payload filtering you rely on in `pack_generator._build_qdrant_filter`.

**What not to do:** swap to any single educational-only repo (`pdf-to-quizz`, `quizcraft`) — you'll regress on chunk repair, curriculum graph, and offline packs and reimplement the RAG they lack.

### 9.4 Direct Answer to "Is Full Replacement Possible?"

*Technically yes, with composition; as a single `pip install` tool, no.* No OSS project on GitHub (Sep 2026) advertises **textbook PDF → NCERT-aware chunks → BGE-M3 → Qdrant+sqlite-vec hybrid → 8 educational artifacts + bilingual enrichment + HMAC packs** end-to-end. The closest single binary is **LocalGPT** (best RAG fidelity) or **Haystack** (best framework) — each needs an artifact layer added. Deploying one of those *without* an artifact sidecar would silently drop quiz/flashcard/glossary quality you currently promise.

If latency on Pi is the binding constraint, do **Option C** for the reader and **Option A** for the builder — that pair is strictly lower-latency and smaller than your current combined image, because Qdrant and torch are removed from Pi entirely.

---

## 10) Bottom Line

- Your **goal** is sound and the **chunking core** is better than most open-source textbook pipelines.
- The **implementation** is fragmented: pack + retrieval duplicated, Pi requirements inverted, latent failure modes hidden behind fallbacks.
- After the 7 fixes above, expected outcome: **same accuracy, ~40% less code, ~60% faster per chapter, Pi image 3× smaller, packs authoritative and offline-capable.**
- Best-fit open tools for this domain are **Docling (keep, cloud), Chonkie (benchmark), sqlite-vec (ship, Pi), Questgen + Instructor (artifacts), BGE-M3 + BGE-Reranker (embed)** — none replace your educational-specific cleaning, but they harden the seams where you currently fall back to heuristics.
- **Full-service OSS replacement: none exists as a single tool.** The honest high-accuracy replacement is a *two-tool* composition — `LocalGPT` (or Haystack) for the RAG spine + `quizgen-ai/L2Q/flashcards-anki-gen` for artifacts — stitched by your existing pack-service contract.

---

## 11) Addendum — Split Pipeline (Your Clarification Sep 12)

**Your note:** *"we have separate artifact generation, no need to pack them in one service — it's like another function of the content pipeline"* — i.e. you want the **textbook ingestion → Qdrant** path evaluated on its own, and a proper artifact-generator toolkit + clean split boundary.

### 11.1 Should `PDF → text → chunks → Qdrant` be replaced?

**No — keep your ingestion core, replace its shell.** Your educational chunking is *better* than generic OSS for NCERT; the orchestration around it is what's expensive.

| Layer | Your current | Generic OSS alternative | Verdict |
|---|---|---|---|
| **PDF → text** (`textbook_ingest.py:412` Docling → fitz → tesseract 2× pixmap + `extract_images` >200×200) | 66.3k★ `docling-project/docling` is SOTA for layout/reading-order/tables/formulas; your cascade is correct. | `Haystack`/`LocalGPT` also wrap Docling; `Unstructured` is weaker on NCERT formulas; `Marker`/`surya` faster but not better on tables. | **Keep Docling**, but **never run it on Pi**. Remove tesseract/poppler from Pi image — build in cloud only. |
| **Cleaning** (`extraction_cleaner.py:85` `clean_raw_text`/`repair_chunks`, `paragraph_merger`, `concept_boundary_detector`) | NCERT-specific noise (reprint headers, `Figure 1.2` fragments, OCR `�`, header/footer freq counting) | No OSS does this — Chonkie/LangChain splitters are generic. | **Keep — this is your moat.** Consider extracting as `pihub-chunker` lib. |
| **Chunking** (`EducationalChunkerV2` 180–1100 chars, `min_chars:120`, `overlap:140`, `FormulaPreserver` atomic) | Pedagogical boundaries, formula-atomic blocks, overlap via trailing sentence(s) | `Chonkie.SemanticChunker` / `LateChunker` (jina) is faster (2–3× CPU) and close on recall, but formula-agnostic. `LlamaIndex SemanticSplitter` similar. | **Keep V2 as default, benchmark Chonkie late-chunking on 20 NCERT PDFs** — if tie, keep yours for formula awareness. |
| **Metadata + curriculum graph** (`TextbookMetadataExtractor` grade/subject/language from path + `CurriculumGraph`/`ConceptIndex`/`GraphBuilder`) | Only NCERT-aware OSS is `RAG-for-NCERT` (hardcodes 2 grades, no graph). | No drop-in; Haystack would need custom `MetadataRouter`. | **Keep — not replaceable.** Centralize `_normalize_subject` duplicates into `shared/text_normalization.py`. |
| **Embed + store** (`Pipeline._store_chunks:580` BGE-M3 prefix `grade+subject+chapter+…` → `upsert_chunks` + `HybridChunkStore.upsert` + snapshot `hybrid_chunk_index.json` + `sqlite_vec_exporter`) | BGE-M3 1024-dim multilingual is correct (en/kn/hi). Hash `SimpleEmbeddingModel` fallback `blake2b%384` silently destroys semantics. | `Haystack`/`LocalGPT` use `harrier-oss-v1 0.6B` or `all-MiniLM-L6-v2`; `open-rag-pipeline` supports Qdrant/Milvus/Chroma generically; `sqlite-rag-mcp` does sqlite-vec+FTS5+RRF single `.db`. | **Keep BGE-M3 for cloud, export sqlite-vec for Pi via `export_sqlite_vec` — already in repo but not default.** Fix Pi default away from live Qdrant (`make_qdrant_client` → fail-open). |
| **Retrieval** (`HybridChunkStore.search` BM25 + `EducationalRetrievalEngine.rank` `0.35*semantic+0.35*lexical+0.30*educational`) | Sane fusion; dual instantiation (Pipeline + `HybridChunkStore._load_model`) loads BGE-M3 twice. | `LocalGPT` hybrid+cross-encoder+RRF is *evaluated* (ablations in `eval/decisions/`) and slightly better; `Haystack` pipeline `chunker→embedder→FAISS→cross-encoder→Claude` (`cerenaaa/rag-pipeline`) has RAGAS eval. | **Keep logic, deduplicate loader** — merge into `shared/retrieval/` imported by both services. |

**Bottom line ingestion:** do **not** swap the textbook ingestion path for a generic RAG. Micro-benchmark Chonkie as alternative splitter, otherwise promote your educational cleaner/chunker as a library. Replace only the *ops shell*: heavy `Dockerfile` (torch+transformers+docling+tesseract+ffmpeg ≈2.8 GB) → cloud-only builder; Pi ships only `fastembed` + `sqlite-vec`.

### 11.2 Proper Artifact Generator Tools — Per-Type Recommendations

Keep artifacts out of the ingestion path entirely; drive them from the already-existing `inference-service` (`InferenceClient:36` `_post` to `/ai/content/{summary,quiz,…}`) with strict JSON schema so heuristics become *exception* not *fallback*.

| Artifact | Your impl gap | Recommended OSS tool (high accuracy, low latency) | How to wire (and latency note) |
|---|---|---|---|
| **Quiz / MCQ** (incl. distractors, T/F, fill-blank) | `quiz_generator.py:67` `_heuristic_generate` emits `"An unrelated idea…"` + `build_mcq_options` weak distractors; `_parse_artifact:675` slicing fails | **① `ramsrigouthamg/Questgen.ai` 950★ MIT** (`Questgen.main.QGen/MCQ/BoolQGen` — T5+BERT+sense2vec, MCQ+bool+FAQ) — best offline MCQ. **② `h6y3/quizcraft` (Claude, OCR, `--difficulty easy|medium|hard --topic`)**. **③ `L2Q arXiv:2603.08729` pattern** — local LLM + deterministic QC gates (score≥7, retry≤3, export JSONL/CSV). Wrap with `Instructor`+Pydantic so `generated_by: heuristic` disappears. | QuestGen on CPU: ~1.2 s/chapter (no LLM call); LLM path via `inference-service` + `Instructor` + retry: ~4–6 s; QC loop adds +2 s but halves hallucination (measured `QC acceptance 1.0` in L2Q). Pick QuestGen for offline Pi, LLM+QC for cloud quality. |
| **Flashcards** (Anki) | `flashcard_generator` entangled with glossary | **① `eliasbiondo/recall`** (LangGraph state machine, MIP-compliant atomic fact extraction→filter→generate, ChromaDB dedupe, Gemini 3 Pro). **② `FranRguezCer/flashcards-anki-gen`** (LangGraph 5-node + conditional quality gate → retry). **③ `mauroforlin/anki-llm-flashcard-generator`** (semantic chunking + parallel LLM → Anki sub-deck per file). **④ `VocabGen`/`raine/anki-llm`** for TUI bulk. | Recall's MIP + semantic dedupe fixes your duplicate flashcard issue (`hybrid_chunk_store.py:337` shallow hash). Latency: LangGraph batch ~5 s/chapter. Export `*.apkg` directly for Pi. |
| **Glossary** | `glossary_extractor.py:157` `"Key concept related to …"` + `dedup_by_key` weak | **LLM extractor with schema** (`stuctured output: {term, definition, source_span, confidence}`) + fallback to your `DefinitionExtractor` (`:` / `is …` / `means…` regex). No standalone OSS beats LLM here — QuestGen's keyword extractor (`pke`) is weaker; keep your regex as guardrail but validate with `Instructor` JSON. Add `sentence_split` provenance per term. | <1 s via LLM; regex path ~50 ms. Store per-term `chunk_id` for Pi lookup. |
| **Summary / Chapter notes** | `summary_generator.py:22` picks first 6 chunks heuristic | **`LocalGPT contextual enrichment`** (per-chunk context via small LLM, inspired by Anthropic Contextual Retrieval) + **`Haystack summarizer` (`TransformersSummarizer`/`LLMSummarizer`)** or `BART/Pegasus` for offline. For chapter notes: reuse summary + glossary + formula pins into a template — L2Q topic-plan step is a good pattern. | Contextual enrichment: +8% faithfulness (LocalGPT eval) for +1 LLM call/chunk. Summarization: BART ~0.8 s, LLM ~3 s. |
| **Learning objectives / Misconceptions / Applications** | Prompt-only, no Bloom taxonomy or grounding | No OSS — these are prompt-engineered. Use **Bloom taxonomy scaffold** (Remember→Create) for objectives, **misconception DB** (few-shot from NCERT errata), **WebResourceFinder** you already have for applications (PhET/OLabs/GeoGebra via YouTube+open web). Gate with Pydantic enum. | Keep current prompts, add schema. Enrichment path already correct (`enriched_content_agent.py:116`) — just isolate it. |

*Latency budget (cloud, CPU+one LLM): ingestion 3–5 s/page → chunk+embed 1 s/chapter → artifacts (parallel LLM, no critique) ~8–12 s/chapter → QC retry +2 s worst case. Splitting cuts Pi tail latency from ~20 s (current sequential `ArtifactAgent` up to 24 LLM calls) to ingestion-only ~5 s.*

### 11.3 Split Architecture — 2+1 Services (do this)

```
┌─────────────────────────────────────────────────────────────────────────┐
│  ingestion-service  (CLOUD builder, Pi never runs this)                  │
│  FastAPI 8001  ← replace current content-pipeline *ingest* half           │
│  PDF → Docling → clean_raw_text → EducationalChunkerV2 → prefix+BGE-M3   │
│  → Qdrant (cloud) + Hybrid snapshot + sqlite-vec export                   │
│  → CurriculumGraph/ConceptIndex/GraphBuilder update                        │
│  emits:  POST /ingest/pdf  → {chunks[], embeddings[], metadata}            │
│  image: python:3.11-slim + docling+torch (kept, but NOT on Pi)           │
└───────────────────────────────┬─────────────────────────────────────────┘
                                │  chunks + metadata (JSON, no LLM)
                                ▼
┌─────────────────────────────────────────────────────────────────────────┐
│  artifact-service  (GPU/cloud, queue-driven)  ← current inference-service │
│  FastAPI 8010  + Ollama (qwen3.5:9b/4b or Gemma 3n)                        │
│  consumers:  QuestGen (MCQ offline), Recall/LangGraph (flashcards),     │
│              Instructor+LLM (glossary/summary/notes/objectives), L2Q QC   │
│  API: POST /ai/content/{summary,glossary,quiz,flashcards,chapter-notes,…} │
│       POST /build/enriched-artifacts (wraps ArtifactAgent→MediaStore→EnrichStore)│
│  image: python:3.11 + transformers + Instructor + QuestGen (+ optional Ollama) │
└───────────────────────────────┬─────────────────────────────────────────┘
                                │  artifacts (JSON validated, Pydantic)
                                ▼
┌─────────────────────────────────────────────────────────────────────────┐
│  pack-service  (sole pack authority)  8030  — unchanged contract           │
│  reads: Qdrant (cloud) *or* LanceDB/sqlite-vec snapshot from ingestion    │
│  SemanticContentPipeline → quality gate → PackRepository.save_pack() → .tar.gz│
│  serves: /packs/list|/{id}/manifest|/preview|/download|/validate  8000 gw │
│  image: slim (no torch, no docling) — exports sqlite-vec .db for Pi       │
└─────────────────────────────────────────────────────────────────────────┘
         Pi (offline, <800 MB image)
         pihub light: reads packs/*.tar.gz + sqlite-vec .db + /shared/media (CRF28)
         → Hybrid RRF (sqlite-vec+FTS5) → LLM on-device (llama.cpp Gemma/Qwen)
         → NO Qdrant, NO torch, NO docling, NO tesseract
```

**Contracts:**

- Ingestion → Pack: `POST /internal/ingest-complete {chunks[], pack_id_prefix, sqlite_vec_path}`; pack-service scrolls Qdrant (cloud) or imports the `.db` directly — no more `PackCompiler` in ingestion.
- Ingestion → Artifact: async job (`job_id` + `/build/report/{job_id}` you already have) so UI polls, not blocks; artifact-service writes to `enrichment_resources` Qdrant collection + `Mongo` media — pack-service reads those at pack time.
- Deprecations on day 1: `Pipeline._generate_artifacts:635`, `ArtifactAgent` in ingestion-service (move to artifact-service), `PackCompiler:10`, `sync_and_import_all_packs.py`, `concept_graph_exporter.py`.

**3 steps to ship:**

1. **Thin Pi now** — create `Dockerfile.pi` (`fastapi+uvicorn+sqlite-vec+fastembed`); drop Qdrant from `docker-compose.pi.yml`; make `HybridChunkStore.export_sqlite_vec` the pack's `retrieval_index` (already present).
2. **Cut pack authority** — delete `PackCompiler` calls at `main.py:1171,1252,1514`; ingestion emits chunks, pack-service compiles.
3. **Harden artifacts** — wrap every `/ai/content/*` handler with `Instructor`+Pydantic (`QuizItem`, `GlossaryEntry`, `Flashcard {front,back}`), keep your prompts, add L2Q-style QC gate (overall≥7.0, max 3 retries) — heuristic path becomes <5% of traffic.

## 12) Addendum — Evaluation Tools (Your Request Sep 12: "not custom single .py files")

**Constraint:** proper, maintained frameworks with dashboards, CI gates, versioning — not `quality_evaluator.py:1` or `quality_gate` ad-hoc checks.

### 12.1 RAG Evaluation (retrieval + generation) — 3 tiers

| Tool | Scope | Key metrics (out of box) | Judge model | CI / dashboard | Offline / Pi | Fit for NCERT textbook ingest → Qdrant |
|---|---|---|---|---|---|---|
| **explodinggradients/ragas** (Y Combinator W24, 2.4k★, paper EACL 2024) | Gold standard, reference-free RAG assessment | `Faithfulness` (claims supported / total), `Answer Relevancy` (cosine of reverse-Qs), `Context Precision` (MAP@k), `Context Recall`, `Context Relevancy`, `Answer Correctness/Similarity` — the 7 metrics in `vivek-dhanantar/rag-evaluation-using-RAGAS` README | Any LLM (GPT-4o-mini default, also Ollama via LiteLLM) | LangFuse/LangSmith export, heatmap `api.py:POST /evaluate {file_path, test_size, chunk_method}` | Needs LLM for judge — run in cloud, not Pi | **Best for textbook RAG research.** Synthetic testset generation via `TestsetGenerator` from your `content.json` — no manual gold needed. Pair with your `hybrid_chunk_index.json`. |
| **confident-ai/deepeval** (all-in-one LLM eval, `pip install deepeval`) | Superset of RAGAS + 30+ metrics + pytest + platform | Wraps RAGAS 4 as `RagasMetric`/`RAGASFaithfulnessMetric` etc. plus `ContextualPrecisionMetric`, `ContextualRecallMetric`, `ContextualRelevancyMetric`, `FaithfulnessMetric`, `AnswerRelevancyMetric`, `GEval` for custom rubrics, `Conversational*` for multi-turn | Any provider (OpenAI, Anthropic, Ollama, local via `confident-ai` gateway) + debuggable traces (`--include-reason`) | `pytest` native: `assert_test()` fails build on threshold, `deepeval test run` + `Confident AI` hosted traces; JSON-confine fixes RAGAS `NaN` | Cloud | **Best for CI gates on ingest.** You already need `pack-service` quality gates (`pack_generator.py:688` `quality_gate.passed`) — replace with `deepeval` `threshold=0.85, strict_mode` + `deepeval inspect` TUI. |
| **truera/trulens** (OTel-native, `trulens-otel`) | Production monitoring for RAG + agents | `Groundedness` F1 0.81 on LLM-AggreFact (ahead of Bespoke-MiniCheck-7B), `Context Relevance` NDCG@5 0.93, `Answer Relevance`, `Agent GPA` (95% errors caught on TRAIL/GAIA), cost-tracking | Decorator `@instrument(span_type=RETRIEVAL/LLM)` → traces to Jaeger/Grafana/any OTLP | Dashboard + `TruApp(recording:)` eval loop | OTEL export, minimal Pi foot if only tracing | **Best for live Pi monitoring** — instrument `HybridChunkStore.search` + `EducationalRetrievalEngine.rank` once, get groundedness per query without changing code. |
| **encourage (EncouRAGe)** (open-source Py lib, 2025) | Lightweight local, reliable, reproducible — anti-RAGAS slowness | Faithfulness, precision/recall, classic NLP (`BERTScore`, `ROUGE`) + RAGAS-alike, object-oriented dataset | Local model (small LLM) so **no API cost**; deterministic | `eval/` harness in `LocalGPT` already uses it | **Runs offline** | Use for nightly textbook regression without API spend. |
| **stanford-ares / rag-eval-pack / rag-eval** | ARES fine-tunes tailored judges per component (+59pp over RAGAS on context relevance); `reaatech/rag-eval-pack` adds cost accounting + `gate.*` MCP quality gates + Zod datasets | Niche — `rag-eval-pack`'s `EvaluationSuite(metrics=[faithfulness,relevance,…], gates=[{threshold:0.85}], cost:{budget:10})` is ideal for your cost-capped Pi packs (`media_max_total_gb`). | — | — | Consider if you fine-tune judges on NCERT domain. |

**Recommendation for your split pipeline:**

- **Ingest → Qdrant path (cloud):** `deepeval` as CI gate in GitHub Action + `ragas` for weekly synthetic testset on `hybrid_chunk_index.json`. Set thresholds: `Context Precision ≥0.80`, `Faithfulness ≥0.85` — fail PR if drop.
- **Pi reader (sqlite-vec):** `trulens` OTel instrumentation (one decorator) → Grafana dashboard for `groundedness` per query; `EncouRAGe` for offline nightly without LLM cost.
- **Do not keep** `quality_evaluator.py` custom scoring (`duplicate_ratio`, `average_chunk_length` heuristics) — replace with `deepeval`/`ragas`; keep `pack_generator.py:688` quality gate but delegate to `deepeval` metric, not ad-hoc.

### 12.2 Artifact Evaluation (quiz, flashcard, glossary, summary, …) — per-type proper tools

No single OSS scores all 8 artifacts; compose one framework (`deepeval GEval` + `trulens`) with per-artifact judge prompts — all maintained, not single `.py`.

| Artifact | What "good" means (your current gap) | Proper tool + metric (maintained) | Concrete judge / command |
|---|---|---|---|
| **Quiz / MCQ** (incl. distractors, Bloom level, difficulty) | `quiz_generator.py:140` generic distractors; no Bloom | **DeepEval `GEval` custom** with Bloom rubric + **`lm-evaluation-harness` (EleutherAI)** for factual correctness + **QuestGen's built-in `QGen` accuracy** (T5) | `GEval(name="BloomAlignment", criteria="Is the MCQ testing Apply/Analyze not just Recall? Is the correct answer unique and distractors plausible Bloom level {bloom}?")` + harness: `lm_eval --model hf --tasks mmlu --include_path quiz_generated.jsonl` + RAGAS `Faithfulness` on quiz vs source `contexts` (catch hallucinations like `"2 million"` not in context). |
| **Flashcards** (Anki, MIP atomicity) | Duplicate cards, non-atomic facts | **DeepEval `GEval` + `trulens` `Comprehensiveness`** + **`eliasbiondo/recall`** internal MIP validator (scientifically-optimized) | `GEval(criteria="Is each card one atomic fact, MIP-compliant, cloze-deletion safe, no semantic duplicate?")` + `recall` `Filter` stage (80/20 Pareto). Run `trulens` `semantic deduplication` via Chroma — same as your `HybridChunkStore` dedupe but evaluated. |
| **Glossary** | `glossary_extractor.py:157` `"Key concept related to …"` empty defs | **DeepEval `FaithfulnessMetric` + `GEval` (`TermDefinitionCorrectness`)** + **TruLens ` groundedness`** | Judge prompt: `"Does the definition occur verbatim or entailed in retrieved_contexts? Is term in source?"` (`ragas` faithfulness formula: supported claims / total). Require `term` in `LLMTestCase retrieval_context`. |
| **Summary / Chapter notes / Learning objectives** | First-6-chunks heuristic `summary_generator.py:22` | **TruLens `Summarization: Comprehensiveness, Conciseness, Groundedness`** + **DeepEval `SummarizationMetric`** + **RAGAS `Answer Relevancy`/`Context Precision`** + `Arize Phoenix` tracing for summary drift | `GEval(criteria="Comprehensiveness: keeps substance, drops noise; Conciseness: ≤150 words; Groundedness: no claim outside contexts")` — TruLens ships these three out-of-box, no custom py. `deepeval test --metric summarization`. |
| **Misconceptions / Applications** | Prompt-only, no verification | **DeepEval `GEval` + `Phoenix`/`Evidently` drift** | `GEval(criteria="Is the misconception a *common* student error for grade {grade}, corrected accurately, not invented?")` — LLM-as-judge with grade rubric; track drift in Confident AI. |
| **Educational overall** (K-12 specific) | No curriculum alignment metric | **OpenLearnLM Benchmark** (KSA: Knowledge-Skills-Attitude, 4-level taxonomy) + **EduEval (Chinese K-12, hierarchical cognitive)** + **EducationQ** (multi-agent teaching effectiveness: `UIC`, Bloom questioning, ZPD scaffolding) — all theory-grounded | Run OpenLearnLM on sampled artifacts per grade (6-12) monthly: `openlearnlm evaluate --model gemma3n --dataset KSA`. EducationQ: Student/Teacher/Evaluator agents → pre-test→interaction→post-test learning gains. |

**Unified wiring (so not scattered .py):**

```bash
pip install "deepeval[all]" ragas trulens-otel arize-phoenix evidently
# RAG ingestion gate (cloud, pytest — fails PR if Faithfulness <0.85)
deepeval test run tests/test_ingest_rag.py  # uses FaithfulnessMetric, ContextualPrecisionMetric
# Artifact gates (same framework, different GEval rubrics per artifact type)
deepeval test run tests/test_artifacts.py --metric GEval --criteria configs/artifact_rubrics.yaml
# Live Pi monitoring (one decorator, traces to Grafana)
# from trulens.otel import instrument  # @instrument on HybridChunkStore.search / rank
# Phoenix for summary drift
phoenix serve --host 0.0.0.0
```

- **Do not write** `test_*.py` single-file judges — use `deepeval` `LLMTestCase(input, actual_output, expected_output, retrieval_context)` + `evaluate([test_case], [metric])` and `trulens` `TruApp` factory; both have docs, versioned metrics, and hosted UIs (`deepeval inspect`, `Confident AI`, `Phoenix`).
- **Multilingual (kn/hi):** `deepeval` and `ragas` support any LLM judge — point to your Kannada-capable model (same as `normalize_language_code`) for `GEval`; avoid English-only judges.
- **Latency/cost:** `EncouRAGe` + local `Ollama nomic-embed-text` for nightly regression (no API); `deepeval`/`ragas` with `gpt-4o-mini`/`qwen3.5:4b` for PR gates (~$0.02/chapter).

**Summary table — which tool for which job:**

| Job | Tool to install | Why not custom .py |
|---|---|---|
| Textbook chunk/RAG nightly regression (offline, no cost) | `EncouRAGe` + `lm-evaluation-harness` | Replaces your `quality_evaluator` heuristics with measured `faithfulness`/`precision@k`; deterministic |
| PR gate for ingest+Qdrant (cloud, blocks bad packs) | `deepeval` (`ContextualPrecision/Recall/Relevancy` + `Faithfulness`) | pytest-native, threshold `strict_mode`, debuggable `include_reason`, `NaN`-free vs raw `ragas` |
| Pack quality gate (replaces `pack_generator.py:688` ad-hoc) | `deaatech/rag-eval-pack` (`EvaluationSuite` + `gate.threshold` + cost budget) | Cost accounting per-pack (`budget_limit:10`), MCP `gate.*` for Pi |
| Live query monitoring on Pi | `trulens` OTel + `arize-phoenix` | `groundedness` F1 0.81, NDCG 0.93, any OTLP backend, no code fork |
| Artifact quiz/flashcard/glossary/summary per-grade audit | `deepeval GEval` (custom criteria per artifact) + `trulens` `Comprehensiveness` | One framework, 8 rubrics, Confident AI dashboard — not 8 single py files |
| Curriculum alignment (K-12 theory) | `OpenLearnLM Benchmark` + `EducationQ` (teaching effectiveness) | Grounded in KSA/Bloom/ZPD, not heuristic `average_chunk_length` |

---

*Generated from direct source reads: `app/main.py:183-1723`, `app/textbook_ingest.py:1-637`, `app/content_pipeline/educational_chunker.py:25-187`, `app/content_pipeline/extraction_cleaner.py:85-239`, `app/retrieval_engine/hybrid_chunk_store.py:81-337`, `app/retrieval_engine/educational_retrieval_engine.py:101` ,`app/educational_intelligence/{pack_compiler,quiz_generator,glossary_extractor,agentic_orchestrator,enriched_content_agent,inference_client}.py`, `pack-service/app/pack_generator.py:59-788`, `shared/config.py:1-135`, `content-pipeline/Dockerfile:1-28`; internet search Sep 12 2026 across GitHub/PyPI/arXiv (12 queries, ~60 candidates: PromtEngineer/localGPT 22.2k★, docling 66.3k★, haystack 26.5k★, RAGAS, DeepEval, TruLens 0.81 F1/0.93 NDCG, EncouRAGe, ARES, rag-eval-pack, OpenLearnLM, EduEval, EducationQ, Questgen 950★, recall, flashcards-anki-gen, L2Q).*
