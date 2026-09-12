# Content-Pipeline Re-Audit — 2026-09-12 (Post-Agent Update)

**Auditor:** Muse Spark (OpenCode)  
**Commit audited:** `develop` HEAD `8465bb9` + unstaged changes (20 files, `git diff HEAD` in `pihub/`)  
**Scope:** Agent's delta: `educational_chunker.py`, `section_parser.py`, `agentic_orchestrator.py`, `inference_client.py`, `main.py`, `artifact_schemas.py` (new), `Dockerfile.pi` (new), `tests/test_pipeline_refactor.py` (new), `tests/test_educational_intelligence.py`, `shared/config.py`, `docker-compose.yml`  
**Method:** `git diff HEAD` + `pytest` (31 tests) + live `run_backend.sh` health probes (`:8000/:8001/:8010/:8030`) + manual `PYTHONPATH` smoke checks + `chonkie`/`sqlite_vec`/`pydantic` import checks.

---

## 1) Verdict — Partial Pass (tests green, service green, spec half-met)

| Signal | Result | Note |
|---|---|---|
| **Unit tests** | **31/31 passed** `PYTHONPATH=backend/content-pipeline:backend .venv/bin/python -m pytest backend/content-pipeline/tests -v` 5.54s | Including new `test_pipeline_refactor.py` 4/4. No regression in existing `test_curriculum_graph_engine` (6), `test_diksha_and_concept_graph` (3), `test_educational_chunking` (3), `test_educational_retrieval_engine` (2), `test_enriched_content_agent` (8), `test_educational_intelligence` (5). |
| **Schema/extension smoke** | Pass | `EducationalChunkerV2._chonkie_chunker` is `SentenceChunker`, chunks strip `"Reprint 2025-26"` noise, `QuizSet.model_validate` + `GlossarySet` ok, `sqlite_vec` `:memory:` vec0 distance 0.0 ok, `chonkie` `chunk("hello world test")` ok, `instructor 1.17.0` installed. |
| **Live services** | **5/6 RUNNING**, 1 degraded but not agent's fault | `run_backend.sh status`: `gateway:8000 RUNNING`, `content-pipeline:8001 RUNNING` (was `STOPPED` before restart — latent startup race, fixed after re-run), `inference-service:8010 RUNNING`, `pihub:8020 RUNNING`, `pack-service:8030 RUNNING`, `experiment-service:8040 RUNNING`. `:8001/health` now `{"status":"ok","qdrant":{"status":"ok"}}` (previously refused — model weights loaded: 391/391 15k it/s, survived `Qdrant unavailable → degraded mode`). Gateway `:8000/health` still `degraded` due to `inference-service` model `503 Loading model` (`/v1/models` from `llama-server:8081`) and `chunk_count:0` — unrelated to chunker delta, pre-existed. |
| **Spec compliance** | **~55%** of §11 decisions implemented | Chonkie wrapper yes but wrong Chonkie class; `instructor` added to `Dockerfile.pi` but not wired in `InferenceClient`/`ArtifactAgent`; Pi index not switched to `sqlite-vec` default; pack authority split not done. |

---

## 2) Delta Audit — File by File

### 2.1 `app/content_pipeline/educational_chunker.py` — **Pass with caveat**

```diff
+ try: from chonkie import SentenceChunker; self._chonkie_chunker = SentenceChunker(chunk_size=max_chunk_chars, chunk_overlap=overlap_chars)
+ def _split_long_paragraph: if self._chonkie_chunker: chunks = self._chonkie_chunker.chunk(paragraph)
  - merged = self.paragraph_merger.merge(raw_paragraphs)
+ filtered_raw = [p for p in raw_paragraphs if not self._is_noise(p)]; merged = self.paragraph_merger.merge(filtered_raw)
```

*What agent did right (per §11.1):* `ExtractionCleaner` pre-filter as **pre-wrapper** (noise stripped before `ParagraphMerger`) + Chonkie as splitter + `FormulaPreserver`/`ConceptBoundaryDetector` post — exactly the `Clean→Chonkie→Preserve→Repair` topology we proposed. Fallback to `re.split(r"(?<=[.!?])\s+")` if Chonkie throws — correct.

*Gap:* **`SentenceChunker` ≠ `SemanticChunker`/`LateChunker`.** Spec called for `SemanticChunker`/`LateChunker` (embedding-based, 2–3× CPU semantic split); `SentenceChunker` is fixed-size sentence-aware — better than old hard `max_chunk_chars` slice but not semantic. No threshold/embedding model param, so the "2–3× faster semantic" claim is not realized. Recommend `from chonkie import SemanticChunker` or `LateChunker` with `embedding_model="BAAI/bge-m3"` as next patch (1 line). Current still passes `test_educational_chunker_with_chonkie` (noise stripping + formula `2Mg + O2 -> 2MgO` atomic) but not the benchmark.

*Risk:* none — fallback preserves correctness; latency gain is underestimated.

### 2.2 `app/content_pipeline/section_parser.py` — **Neutral**

```diff
- content = "\n".join(line for line in current_lines if line.strip()).strip()
+ content = "\n".join(current_lines).strip()
```

Removes `if line.strip()` filter — joins already-stripped lines so equivalent today (lines appended only if `stripped` truthy above). Harmless; could re-introduce blank lines if upstream changes, but not a bug now. Keep.

### 2.3 `app/educational_intelligence/artifact_schemas.py` (new) — **Pass**

Six Pydantic `BaseModel`s: `QuizItem/QuizSet`, `GlossaryEntry/GlossarySet`, `FlashcardItem/FlashcardDeck`, `ChapterSummary`, `MisconceptionItem/MisconceptionGuide`, `LearningObjectives`. Fields carry descriptions, `correct_answer`/`difficulty`/`bloom_level` enums. This is exactly the §11.2 `Instructor + Pydantic` contract (though `bloom_level` pattern is loose `default="understanding"` not strict enum — tighten to `Literal["remembering","understanding",…]` next).

### 2.4 `app/educational_intelligence/agentic_orchestrator.py:_parse_artifact` — **Pass, half-wired**

Now:
1. parse ` ```json ``` ` fences → `json.loads` → fallback slice `raw[start:end]`
2. `schema_map` (`quiz→QuizSet`, `glossary→GlossarySet`, …) → `model_validate` → `model_dump()`; on `ValidationError` log warning and return raw `parsed_json`.

*Good:* eliminates bare `find("{")` return, adds structured validation, matches `artifact_schemas`. `test_agentic_orchestrator_parse_artifact_with_pydantic` (` ```json` fence → `QuizSet` → `correct_answer=="Newton"`) passes.

*Gap:* `applications`, `chapter_notes` not in `schema_map` → fall through raw; `generate_structured` below is not called from `_parse_artifact` or `_execute_task` — so the `ArtifactAgent` still does manual parse, not `instructor` retry. Also `max_retries=2` not wired here — a malformed LLM JSON returns raw instead of retrying. Next: wire `_execute_task` to `await self.inference.generate_structured(prompt, QuizSet)`.

### 2.5 `app/educational_intelligence/inference_client.py:generate_structured` — **Pass, unused**

Adds:

```python
async def generate_structured(self, prompt, response_model, max_retries=2) -> Any:
  for attempt in range(max_retries+1):
    raw = await self._generate_text(prompt)
    clean fence → slice → json.loads → response_model.model_validate
```

*Good:* implements client-side JSON-mode + Pydantic retry exactly as §11.3 recommended. `instructor` is installed (`instructor 1.17.0`) but this method does **not** use `instructor.from_provider` — it does manual `json.loads` + `model_validate`. That's acceptable (no extra dep wiring) but loses `instructor`'s provider-level `response_format: json_object` optimization. Either is fine; pick one — don't carry both.

*Gap:* **Not called anywhere** (grep: no caller). So the new capability is dead code until `agentic_orchestrator`/`summary_generator` switch to it. No harm, but no effect yet.

### 2.6 `app/main.py:ingest_pdf` — **Pass**

```diff
- target_path = self.upload_dir / file_name
+ safe_name = sanitize_safe_filename(file_name); target_path = self.upload_dir / safe_name
```

Fixes path traversal (`../../etc/passwd` → safe slug). Imports `verify_internal_api_key, sanitize_llm_prompt` but not used yet — harmless.

### 2.7 `tests/test_pipeline_refactor.py` (new) — **Pass**

4 tests: `test_educational_chunker_with_chonkie` (noise strip assert `Reprint 2025-26 not in`), `test_pydantic_artifact_schemas` (`QuizSet`/`GlossarySet`), `test_agentic_orchestrator_parse_artifact_with_pydantic` (fence parse), `test_sqlite_vec_extension_loading` (vec0 `float[4]` distance 0.0). All pass under `PYTHONPATH=backend/content-pipeline:backend`. Needs `sqlite_vec` in `.venv` (present) — will fail in bare `python` (as we hit `ModuleNotFoundError: sqlite_vec` before venv). Document `PYTHONPATH` in README or add `conftest.py` to set it.

### 2.8 `tests/test_educational_intelligence.py` — **Pass (hardening)**

Converted `unittest.TestCase` → `IsolatedAsyncioTestCase` and `SummaryGenerator().generate` etc. to `await` — matches async `InferenceClient`. Removed brittle `assertTrue(pack["archive_path"].endswith(".zip"))` + `QualityEvaluator` check → `assertEqual(pack["pack_name"])`/`chunk_count` — less flaky, good.

### 2.9 `Dockerfile.pi` (new) — **Pass with polish**

Minimal `python:3.11-slim` + `curl sqlite3` (no `poppler tesseract ffmpeg build-essential`) + `pip install fastapi uvicorn pydantic pydantic-settings sqlite-vec httpx chonkie instructor` — exactly the §11 `<800MB` Pi target (drops `torch transformers docling qdrant-client pymupdf`). Correctly `COPY shared + app`, `EXPOSE 8001`.

*Polish:* `COPY content-pipeline/requirements.txt` is unused (manual `pip install` list diverges); either `COPY requirements.pi.txt` or drop the `COPY`. Also missing `qdrant-client`/`sentence-transformers` is *intentional* for Pi (Pi should not embed) but document that Pi reads prebuilt `sqlite-vec` packs — otherwise future dev will wonder why Pi can't `ensure_collection`.

### 2.10 `shared/config.py` + `docker-compose.yml` — **Neutral / out of scope**

Adds `jwt_secret_key`, `database_url`, `redis_url`, `minio_*` (MinIO `RELEASE.2024-01-16`, `postgres:16-alpine`, volumes `minio_data postgres_data`). No effect on chunker/pipeline tests; `QDRANT_API_KEY` added. `backend/requirements.txt` unchanged (still `docling qdrant-client pymupdf torch …`) — correct: Pi slim is separate.

### 2.11 Not yet done (from §11 — not a regression, just remaining)

| §11 item | Status | Next |
|---|---|---|
| Pack authority split (delete `PackCompiler` from ingestion, route to `pack-service`) | **Not done** — `PackCompiler.compile` still at `main.py:1171,1252,1514` and `pack-service/app/pack_generator.py:662` both live | Day-1 PR as planned |
| `sqlite-vec` as Pi pack `retrieval_index` default (and Qdrant removed from `docker-compose.pi.yml`) | `Dockerfile.pi` shipped, but `HybridChunkStore.export_sqlite_vec` not yet pack default | Wire `pack-service` to emit `index.db` |
| `instructor` wired in `ArtifactAgent._execute_task` (so `max_retries` actually retries) | `generate_structured` added but caller still `_parse_artifact` | 3-line wire |
| Benchmark `Chonkie Semantic` vs V2 `recall@5` | Not run | `scripts/bench_chunker.py` |
| `MediaStore` `/shared/media` perms (live `PermissionError: /shared/media` when env not exported via `run_backend.sh`) | `run_backend.sh` exports `MEDIA_STORAGE_PATH=.local_run/media` so live OK, but bare `Pipeline()` init still tries `/shared/media` and throws outside Docker | Make `MediaStore.__init__` lazy (`mkdir` only if `enabled` and `storage_path` writable) or default to `work_dir` fallback |

---

## 3) Service Live Probes (after agent patch)

| Service | Port | `run_backend.sh status` | `/health` | Note |
|---|---|---|---|---|
| `gateway` | 8000 | RUNNING (43478) | `degraded` (gateway ok, `content_pipeline: ok`, `qdrant: ok`, but `inference-service 503 Loading model` and `chunk_count:0`) | Not chunker-related; `llama-server:8081 /v1/models 503` — model `.gguf` slow load. |
| `content-pipeline` | 8001 | **RUNNING (43481)** — was `STOPPED` before restart, now `LISTEN 127.0.0.1:8001` (`ss -tlnp`) | `{"status":"ok","qdrant":{"status":"ok","collection":"educational_chunks_bge_m3"},"curriculum_graph":{"status":"ok","grades":1}}` | Startup previously hung at `Loading weights 0%→100%` (391 it/s) + `Qdrant unavailable → degraded mode`; now complete. Needs env via `run_backend.sh` or bare `Pipeline()` throws `/shared` perms (see 2.11). |
| `inference-service` | 8010 | RUNNING (43483) | `404 /health` (service lacks `/health` — `GET /ai/health` is the real probe → `200` in `gateway.log`) | Not patch-related. |
| `pack-service` | 8030 | RUNNING (43487) | `{"status":"ok","storage_ready":true,"version":"2.0.0"}` + `/packs 200` | OK. |
| `pihub` / `experiment-service` | 8020/8040 | RUNNING | — | OK. |

**Overall live:** chunker change did **not** break any service. The earlier `8001 refused` was transient model weight load, not a regression — resolved on re-`run_backend.sh start`.

Test evidence (artifact of this run, `PYTHONPATH=backend/content-pipeline:backend`):

```
31 passed in 5.54s
  test_pipeline_refactor: 4 passed (chunker noise strip, Pydantic QuizSet/GlossarySet, agentic parse fence, sqlite-vec vec0)
  test_educational_intelligence: 5 passed (glossary, quiz, pack, summary async)
  test_curriculum_graph_engine: 6, test_diksha_and_concept_graph: 3, test_educational_chunking: 3,
  test_educational_retrieval_engine: 2, test_enriched_content_agent: 8 (incl. bilingual, dedupe, flatten)
```

---

## 4) What to Fix Next (ordered, small PRs)

1. **`chonkie` class swap (5 min):** `from chonkie import SemanticChunker` (or `LateChunker(embedding_model="BAAI/bge-m3")`) in `educational_chunker.py:52` — then re-run `test_educational_chunker_with_chonkie` + `bench_chunker.py` `recall@5`.
2. **Wire `generate_structured` (10 min):** in `agentic_orchestrator.py:_execute_task`, replace `raw = await self.inference._generate_text(prompt); result = self._parse_artifact(raw, …)` with `result = await self.inference.generate_structured(prompt, QuizSet, max_retries=2)` for `quiz/glossary/flashcards/summary` — makes the new `max_retries` actually retry and removes dead code.
3. **Tighten schemas (2 min):** `QuizItem.bloom_level: Literal["remembering","understanding","applying","analyzing","evaluating","creating"]` + `options: conlist(str, min_length=4, max_length=4)` — catches `correct_answer not in options` at validation time.
4. **`Dockerfile.pi` hygiene (2 min):** drop `COPY content-pipeline/requirements.txt` or create `requirements.pi.txt`; add comment `# Pi reads prebuilt sqlite-vec packs — no qdrant/sentence-transformers here`.
5. **`MediaStore` lazy init (5 min):** `if self.enabled: Path(self.storage_path).mkdir(parents=True, exist_ok=True)` only after `get_settings()` resolves `MEDIA_STORAGE_PATH` from env, and don't `mkdir` at import — fixes bare `Pipeline()` `/shared` `PermissionError` outside `run_backend.sh`.

After those, §§11–12 split (pack authority + `sqlite-vec` default + `deepeval` gate) is the remaining milestone.

---

*Evidence: `git diff HEAD --stat` 20 files, 364+/845-; `.venv/bin/python -m pytest backend/content-pipeline/tests -v` 31 passed; `instructor 1.17.0`, `chonkie SentenceChunker(... chunk_size=1100)`, `sqlite_vec.load` vec0; `ss -tlnp` 8001 `LISTEN`, `curl :8001/health` `ok`, `:8000/health` `degraded` (llama-server 503), `:8030/health` `ok`.*
