# Experiment-Service Audit — `pihub/backend/experiment-service`

**Scope:** `app/main.py` (83) + `api/routes.py` (233) + `app/classroom/routes+repository+services` + `app/builder/routes` + `app/manifest/routes` + `services/experiment_run_service` + `repositories/sqlite_run_repository` + `runtime/` + `storage/` + `app/sharing/` + `analytics/`  
**Date:** 2026-09-12

---

## 1. Duplicate Logic

| ID | File:Line | Finding | Fix |
|---|---|---|---|
| | `app/classroom/repositories/classroom_repository.py:11-42` vs `pihub/cache/store.py:42` + `pihub/network/discovery:39` | Same `ClassroomSession` name, different fields (physical hotspot `network_interface/local_ip` vs pedagogical `teacher_id/title/active`). Path collision `/classroom/sessions` vs `/network/session` ambiguous at gateway. | Rename experiment-service to `AssignmentSession`. |
| | `services/experiment_run_service.py:34-135` (create→running→completed) vs `pihub/cache/store.py:607-757` `quiz_sessions` (same state machine) vs `repositories/sqlite_run_repository.py:29` `experiment_runs` | 3 submission/progress tables `experiment_runs/run_events/results` + `classroom_submissions` — no shared lib. `advance_quiz_session:735` mirrors `complete_run:108`. | Shared `RunState` lib. |
| | `api/routes.py:39-121` `GET /experiments`/`/experiments/search` vs `app/experiment_content/routes.py:13-64` `GET /experiments/catalog`/`/experiments/{id}/download` | 2 experiment catalog APIs — frontend only calls `GET /experiments/catalog` via `phet_catalog_service:91`. | Single `/experiments/catalog`. |
| | `app/manifest/` legacy (`models,routes,manifest_service`) vs `app/builder/routes.py` `/builder/manifests` + `storage/manifest_storage_repository` | 2 manifest APIs `/manifests` vs `/builder/manifests` same lifecycle. | One `/builder`. |
| | `app/sharing/routes.py:30-65` `export/import/verify/sign/trust` vs `pihub/distribution/pack_manager` share package | 2 share/trust stores (`sharing/trust` no admin gate, `trust_device` admin). | Single trust. |

---

## 2. Bugs — Validation

| File:Line | Severity | Bug |
|---|---|---|
| `api/routes.py:39-70` filters | MEDIUM | `Query(default=[])` mutable default for `required_sensors/execution_modes/tags` — shared across requests. |
| `app/builder/routes.py:36-44` `create_builder_manifest` payload `dict` → `_parse_model` | HIGH | Bypasses FastAPI validation, partial `UpdateBuilderManifestRequest` not validated. |
| `services/experiment_run_service.py:41-47` `create_run` | MEDIUM | `if not request.x` lets `experiment_id=""` pass — no UUID format check. |
| `repositories/sqlite_run_repository.py:136-148` `_row_to_run` | MEDIUM | `_validate_model` without try — malformed enum crashes vs 400. |
| `app/manifest/routes.py:45-145` 6 of 11 routes (`templates`, `scene/validate`, `resolve`, `versions`) | MEDIUM | No Dart caller (`experiment_manifest_api_service:12,20,28,40` misses them) — dead surface. |

---

## 3. Bugs — Security

| File:Line | Severity | Bug |
|---|---|---|
| `api/routes.py:171-182` IDOR | CRITICAL | `x-user-id/x-user-role` header client-controllable; if header omitted, check **skipped** (`if x_user_id and ...`) — bypass. Only `GET /experiment-runs/student/{id}` protected, `POST /experiment-runs` creates for any `student_id` without auth. |
| `app/classroom/routes.py:30-112` | CRITICAL | No `Depends(require_admin)` nor JWT — anyone `POST /classroom/sessions`, `POST /classroom/sessions/{id}/assignments`, `submit` arbitrary `student_id`. |
| `app/sharing/routes.py:30-65` | HIGH | No admin gate on `sign_package`/`trust` — mutates global trust. |
| `app/main.py:61-64` `StaticFiles` `/simulations` `/shared/packs/...` | HIGH | No auth, `StaticFiles` follows symlinks — traversal `/simulations/../storage/classroom.sqlite3`. |

---

## 4. Bugs — Race / IDOR Path Mismatch

| File:Line | Bug |
|---|---|
| `api/routes.py:39-70` `list_experiments` etc re-import `APIRouter` at line 169 shadowing earlier — merge artifact, dead line. |
| `app/ai/routes.py:30-48` `POST /ai/generate-experiment` vs Dart `ai_experiment_api_service:14` `POST /ai/experiment/generate` | Path reversed — 404. |
| `app/core/database.py:44-55` `sqlite_transaction: begin immediate` correct, but `classroom_repository:99` reads without `begin` vs `create_session:77` transactional — `ensure_student:141` `insert or ignore` race with `create_submission:156` transactional (masked by unique index). |
| `repositories/sqlite_run_repository.py:54-80` `create_run` insert then separate `get_run` connection — lost update if concurrent `complete_run:118-134` reads then updates without `begin immediate`. `VALID_TRANSITIONS:23-31` not enforced in DB. |

---

## 5. Dead Code

`runtime/experiment_runtime.py:9` `ExperimentRuntime` ABC 0 impls, `repositories/experiment_repository.py` legacy duplicated vs `services/experiment_registry.py`, `analytics/experiment_analytics_service` vs `classroom_repository.analytics:181` 2 analytics not merged.

---

## 6. Not-Wired vs Mentora

Wired (5/9 `experiment-runs`): `POST /experiment-runs`, `GET /experiment-runs/student/{id}`, `GET /experiment-runs/{id}`, `POST /experiment-runs/{id}/events`, `POST .../complete`, `GET /analytics/student/{id}` via `experiment_api_service:19`.  
**Not wired:** `GET /experiment-runs/{id}/events`, `GET /analytics/experiment/{id}`, `GET /analytics/system`, `GET /experiment-metrics`, `GET /experiments/{id}/download|/certification`, `POST /ai/generate-experiment` (path mismatch), `POST /sharing/*`, all `classroom/sessions` classroom routes (only via `ClassroomService` mock).

---

**Priority:** Gate classroom + IDOR with JWT (`Depends(require_admin)`), fix `StaticFiles` traversal, align `ai/experiment/generate` path, add ID validation + try-validate model, dedupe `ClassroomSession` rename, add `begin immediate` on `complete_run`.

*Evidence: 233-line `api/routes.py` double import at 169, legacy `IDP/offline_tutor_app` vs Mentora still calls `GET /api/v1/simulations/{chapter}` mismatched to `/simulations/{path}`.*
