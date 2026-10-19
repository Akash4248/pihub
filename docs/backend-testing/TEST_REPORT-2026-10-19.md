# PIHUB Backend — Test Report

**Date:** 2026-10-19
**Scope:** gateway, content-pipeline, inference-service, pihub, pack-service,
experiment-service (+ Qdrant/Mongo dependency stores)
**Code under test:** the current working tree (security-hardened branch
`develop`, commit `d4ea7cf`) — mounted directly into each service's existing
container image, so the running code is today's source, not the 2-month-old
image.
**Harness:** `backend/scripts/tests/harness.py` (standard library only) +
`backend/scripts/tests/svc.sh`.
**Raw data:** `docs/backend-testing/results/*.json`.

---

## 1. Environment

| Item | Value |
|------|-------|
| Host | Ubuntu 24.04, 16 vCPU, 15 GiB RAM (≈10 GiB used by other desktop apps) |
| Container runtime | Docker via snap (`/snap/bin/docker`) |
| Service runtime | Python 3.11 (`python:3.11-slim` images), single-process `uvicorn` |
| Vector store | Qdrant v1.13.4 — collection `educational_chunks`, **6057 points**, dim 384 |
| Embedding model | BAAI/bge-small-en-v1.5 (loaded from `/shared/models/...`) |
| Auth model | Fail-closed `InternalAuthMiddleware` + `require_admin` (see `shared/security.py`) |
| Test method | Current source bind-mounted over `/app/app` (or `/app`), runtime `pip install` of newly-added light deps (`pyjwt`, `filelock`, `pymongo`) since the images predate the hardening |

> The 2-month-old images are missing `PyJWT` and (for pack-service) `filelock`,
> both added to the current code/requirements. They were installed at container
> start so that today's source runs. A production deploy must **rebuild** the
> images from the current `requirements.txt` (see Recommendations).

### Service inventory

| Service | Port | Health path | Routes | Notes |
|---------|------|-------------|--------|-------|
| gateway | 8000 | `/health` | 98 | Front door; `/health` fans out to all downstream services |
| content-pipeline | 8001 | `/health` | 27 | Loads the embedding model at startup (~9 s) |
| inference-service | 8010 | `/ai/health` | 15 | LLM server bridge (`/health` does **not** exist) |
| pihub | 8020 | `/health` | 67 | SQLite-backed classroom node |
| pack-service | 8030 | `/health` | 29 | Pack storage + registry |
| experiment-service | 8040 | `/health` | 59 | Simulation catalogue |

Total **295 routes** across the 6 services.

---

## 2. Smoke tests

Each service was started from a clean container and probed for `/health` +
OpenAPI reachability.

| Service | Health | Latency | Routes | Result |
|---------|-------:|--------:|-------:|:------:|
| gateway | 200 | — | 98 | ✅ |
| content-pipeline | 200 | — | 27 | ✅ |
| inference-service | **401** | — | 15 | ⚠️ health requires auth (Finding H1) |
| pihub | 200 | — | 67 | ✅ |
| pack-service | 200 | — | 29 | ✅ |
| experiment-service | 200 | — | 59 | ✅ |

content-pipeline `/health` reports `qdrant: ok`, `curriculum_graph: ok (11 grades)`.
inference-service `/ai/health` (with key) reports `status: degraded`,
`server: missing_model` (expected — no GGUF loaded in this environment).

---

## 3. Authentication & authorization

Every request is supposed to pass `InternalAuthMiddleware` (fail-closed). A
request is accepted only with a valid `X-Internal-Service-Key` or a valid JWT,
except for public paths (`/health*`, `/docs`, `/openapi.json`, `/metrics`, …).

Probe = a non-public `GET` route: request with **no** credentials, then with a
**wrong** key, then with the **correct** internal key.

| Service | Probe endpoint | No creds | Wrong key | Correct key | Verdict |
|---------|----------------|:--------:|:---------:|:-----------:|:-------:|
| gateway | `/packs` | **200** | **200** | 200 | ❌ **bypass (B1)** |
| content-pipeline | `/ingest/status` | 401 | 401 | 422¹ | ✅ |
| inference-service | `/ai/health` | 401 | 401 | 200 | ✅ |
| pihub | `/classroom` | 401 | 401 | 200 | ✅ |
| pack-service | `/packs/list` | 401 | 401 | 200 | ✅ |
| experiment-service | `/experiments/catalog` | 401 | 401 | 200 | ✅ |

¹ `422` = middleware accepted the request; the endpoint then complained about a
missing required query param. Auth itself passed.

### 🔴 Finding B1 — Gateway auth middleware is a no-op (CRITICAL)

`gateway/app/main.py:104-108` passes `public_prefixes=(..., "/")` to
`InternalAuthMiddleware`. Its matcher is:

```python
def _is_public(self, path):
    return any(path == p or path.startswith(p) for p in self.public_prefixes)
```

`path.startswith("/")` is **true for every path**, so the gateway treats *all*
routes as public and never enforces authentication. Only routes that add their
own dependency (e.g. `/sync`, `/upload`, `/admin/reset-rag`) are protected;
everything else (`/packs`, `/tutor/capabilities`, `/rag/*`, `/discovery`, …) is
reachable anonymously.

Evidence:

```
/sync                no-key=401   (route-level dependency)
/packs               no-key=200   ← should be 401
/tutor/capabilities  no-key=200   ← should be 401
/discovery           no-key=200   ← should be 401
POST /upload         no-key=403   (require_admin dependency)
```

**Fix:** drop `"/"` from the gateway's `public_prefixes` (or make the matcher
treat `"/"` as an exact match only). `"/"` was presumably added to allow the
root health page.

### 🟠 Finding H1 — inference-service health endpoint is not public

`inference-service/app/main.py:404` calls `InternalAuthMiddleware()` with the
default prefixes, which include `/health` but **not** `/ai/health`. The Compose
health-check (`docker-compose.yml:112`) probes `/ai/health` **without a key**, so
in the real deployment the container would be permanently marked *unhealthy*.
Same pattern is why the gateway's own downstream probe reports it unhealthy
(below).

**Fix:** add `/ai/health` to the service's `public_prefixes` (and keep it
read-only), or make the health-check send the internal key.

### 🟠 Finding H2 — gateway downstream health probes are unauthenticated

The gateway `/health` aggregate calls each service's health endpoint **without**
the internal key, so any downstream that enforces middleware reports as
unhealthy:

```
content_pipeline : ok
inference_service: {"detail":"Unauthorized. Provide a valid Bearer token or X-Internal-Service-Key."}
pack_service     : {"detail":"Unauthorized. Provide a valid Bearer token or X-Internal-Service-Key."}
experiment_service: {"healthy": false, "status_code": 401}
pihub            : idle (ok)
```

**Fix:** have the gateway health fan-out attach `X-Internal-Service-Key`
(the gateway already injects it on other hops).

---

## 4. Integration tests

| Test | Result | Evidence |
|------|:------:|----------|
| Gateway → content-pipeline RAG retrieval | ✅ | `GET /rag/chapter?chapter=Fractions&limit=3` → 3 hits, top score **0.805** |
| Gateway → pack-service | ✅ | `GET /packs` → 200 |
| content-pipeline → Qdrant | ✅ | `/health` → `qdrant: ok`; `/ingest/status` returns the corpus (320 files under `/shared/content`) |
| content-pipeline embedding model load | ✅ | `Successfully loaded BAAI/bge-small-en-v1.5 (dim=384)` in ~9 s |
| Gateway aggregate health | ⚠️ | `status: degraded` due to H2 (all downstream *logic* is fine) |
| Gateway downstream auth propagation | ❌ | see H2 |

Representative `GET /health` (gateway):

```json
{"status":"degraded","chunk_count":6057,
 "checks":{"content_pipeline":{"status":"ok"},
           "inference_service":{"detail":"Unauthorized..."},
           "pack_service":{"detail":"Unauthorized..."},
           "experiment_service":{"healthy":false,"status_code":401},
           "pihub":{"status":"idle"},
           "qdrant":{"status":"ok"}}}
```

---

## 5. Load test

Constant **10 concurrent clients for 10 s** per service (each client opens a new
TCP connection per request — a deliberately harsh client).

| Service | Endpoint | req/s | errors | p50 | p95 | p99 |
|---------|----------|------:|-------:|----:|----:|----:|
| pack-service | `/health` | **1772** | 0 % | 5.5 ms | 6.7 ms | 7.4 ms |
| inference-service | `/ai/health` | **1403** | 0 % | 6.9 ms | 8.1 ms | 9.4 ms |
| experiment-service | `/health` | **654** | 0 % | 14.7 ms | 18.4 ms | 24.2 ms |
| content-pipeline | `/health` | **282** | 0 % | 35.3 ms | 46.6 ms | 53.2 ms |
| pihub | `/health` | **270** | **45 %** | 31.5 ms | 58.2 ms | 81.7 ms |
| gateway | `/health` | **32** | 0 % | 241 ms | 647 ms | 862 ms |

Notes:
* **gateway `/health` is a fan-out** (5 downstream HTTP calls), so its throughput
  is an order of magnitude lower and its latency is the *sum* of downstream
  latencies. This is expected for an aggregate health endpoint, but it is not a
  cheap liveness probe.
* **pihub** returned HTTP 500 on ~45 % of requests under 10-way concurrency
  (see M1).

---

## 6. Stress test (concurrency ramp)

Levels `1,5,10,25,50` concurrent, **5 s each**. `p95-limit = 2000 ms`.

### pack-service (no break)
| C | req/s | err | p95 |
|--:|------:|----:|----:|
| 1 | 1186 | 0 % | 1.0 ms |
| 5 | 1617 | 0 % | 3.8 ms |
| 10 | 1664 | 0 % | 7.1 ms |
| 25 | 1753 | 0 % | 16.2 ms |
| 50 | 1842 | 0 % | 30.6 ms |

### inference-service (no break)
| C | req/s | err | p95 |
|--:|------:|----:|----:|
| 1 | 1075 | 0 % | 1.1 ms |
| 5 | 1314 | 0 % | 4.4 ms |
| 10 | 1278 | 0 % | 8.9 ms |
| 25 | 1270 | 0 % | 21.6 ms |
| 50 | 1238 | 0 % | 44.3 ms |

### experiment-service (no break)
| C | req/s | err | p95 |
|--:|------:|----:|----:|
| 1 | 699 | 0 % | 1.8 ms |
| 5 | 670 | 0 % | 8.6 ms |
| 10 | 661 | 0 % | 17.6 ms |
| 25 | 621 | 0 % | 46.7 ms |
| 50 | 626 | 0 % | 107.9 ms |

### content-pipeline (no break)
| C | req/s | err | p95 |
|--:|------:|----:|----:|
| 1 | 94 | 0 % | 19.4 ms |
| 5 | 235 | 0 % | 32.8 ms |
| 10 | 253 | 0 % | 51.0 ms |
| 25 | 265 | 0 % | 120.3 ms |
| 50 | 265 | 0 % | 219.9 ms |

### 🔴 pihub — breaks at C = 10
| C | req/s | err | p95 |
|--:|------:|----:|----:|
| 1 | 319 | 0 % | 4.8 ms |
| 5 | 388 | 0 % | 21.0 ms |
| **10** | 298 | **42 %** | 52.7 ms |
| 25 | 245 | **68 %** | 151.0 ms |
| 50 | 297 | **58 %** | 300.0 ms |

### 🟠 gateway — p95 crossing 2 s at C = 50
| C | req/s | err | p95 |
|--:|------:|----:|----:|
| 1 | 37 | 0 % | 41 ms |
| 5 | 44 | 0 % | 161 ms |
| 10 | 38 | 0 % | 378 ms |
| 25 | 27 | 0 % | 1416 ms |
| **50** | 26 | 0 % | **3269 ms** |

**Breaking points:** pihub @ 10 concurrent; gateway @ 50 concurrent (p95 > 2 s);
pack / inference / experiment / content-pipeline did not break within 50×.

### 🔴 Finding M1 — pihub `/health` returns HTTP 500 under concurrency

At C ≥ 10, pihub emits `sqlite3.OperationalError: unable to open database file`
on 40–68 % of requests:

```
File "/app/api/main.py", line 188, in health
  system_health = health_monitor.get_system_health()
File "/app/monitoring/health.py", line 18, in get_system_health
  devices = self.store.list_devices()
File "/app/cache/store.py", line 388, in list_devices
  with self._connect() as connection:
File "/app/cache/store.py", line 25, in _connect
  connection = sqlite3.connect(self.db_path, timeout=10.0)
sqlite3.OperationalError: unable to open database file
```

`PiHubStore._connect()` opens a brand-new SQLite connection **per call** and
re-executes `PRAGMA journal_mode = WAL` each time. Under concurrency the
per-request connection churn exhausts resources / races on WAL creation,
producing `SQLITE_CANTOPEN`. There is no connection pooling.

**Suggested direction:** open the SQLite connection once (or use a
thread-local/pooled connection), set WAL once at init, and stop re-issuing
`journal_mode` per request.

---

## 7. Findings summary

| ID | Severity | Service | Finding | Status |
|----|----------|---------|---------|:------:|
| **B1** | 🔴 Critical | gateway | `public_prefixes` contains `"/"` → middleware matches every path → **auth bypass gateway-wide** | ✅ Fixed |
| **H1** | 🟠 High | inference-service | `/ai/health` not public → Compose health-check (no key) always fails | ✅ Fixed |
| **H2** | 🟠 High | gateway | downstream health fan-out sends no internal key → false "unhealthy" | ✅ Fixed |
| **M1** | 🟠 High | pihub | `/health` HTTP 500 under ≥10 concurrent (per-request SQLite connect, no pooling) | ✅ Fixed |
| **L1** | 🟡 Low | ops | gateway `/health` fans out synchronously to 5 services (p95 > 2 s @ 25×) — use a cheap liveness probe | Open |
| **L2** | 🟡 Low | ops | images predate the hardening (`PyJWT`/`filelock` absent) → must rebuild | Open |
| — | ℹ️ | inference | `/ai/health` reports `missing_model` in this env (expected, no GGUF) | Expected |

See §10 for the fixes and the post-fix re-verification.

## 8. Recommendations

1. **Fix B1 immediately** — remove `"/"` from the gateway `public_prefixes`
   (or make `_is_public` treat `"/"` as exact-only). Re-test: `/packs`,
   `/tutor/capabilities`, `/discovery` must return `401` without credentials.
2. **Add `/ai/health`** to inference-service's public prefixes (or make all
   health probes send the internal key), and update the Compose health-check.
3. **Authenticate the gateway health fan-out** with `X-Internal-Service-Key`.
4. **Fix pihub SQLite connection handling** (pool / single connection, WAL once).
5. **Rebuild all service images** from the current `requirements.txt` before
   deploying (the images tested were 2 months old; the hardening deps were
   installed at runtime here).
6. Consider a **lighter liveness endpoint** on the gateway (e.g. `/livez` that
   returns immediately) separate from the expensive aggregate `/health`.
7. Re-run the load/stress suite after fixing M1 and B1 to confirm regression-free.

## 9. Limitations

* Single-process `uvicorn` per service (no `--workers`); results are per-process,
  not per-replica. Multi-worker/gateway throughput was **not** measured.
* The load client opens a fresh TCP connection per request; production clients
  with keep-alive will see higher throughput / lower latency.
* Host was shared with other desktop workloads (≈10 GiB RAM in use); absolute
  numbers are indicative, not benchmark-grade.
* `voice-service` has **no container image** and heavy deps
  (`torch`, `faster-whisper`, `llama-cpp-python`); it was **not** exercised.
* Each load run was 10 s (load) / 5 s (stress); no multi-hour soak test.
* `experiment-service` was mounted from a writable copy because it writes SQLite
  state into its own package tree.

## 10. Fixes applied & re-verification (2026-10-19, same day)

### Code changes

| Finding | File | Change |
|---------|------|--------|
| B1 | `shared/security.py` | `InternalAuthMiddleware._is_public()` now treats a `"/"` prefix as an **exact-match only**; `startswith("/")` no longer marks every path public. This protects every service from the footgun regardless of how `public_prefixes` is written. |
| H1 | `inference-service/app/main.py` | `InternalAuthMiddleware` now receives explicit `public_prefixes` that include `/ai/health` (read-only liveness probe). |
| H2 | `gateway/app/main.py` | `_safe_http_get_json()` attaches `X-Internal-Service-Key: get_internal_service_key()` to every downstream health probe. |
| H2 | `gateway/app/services/experiment_service_client.py` | `health()` and `_request()` send the internal key on all gateway→experiment-service hops. |
| M1 | `pihub/cache/store.py` | `PiHubStore` now enables WAL **once** at startup (`_enable_wal`) and reuses **one SQLite connection per worker thread** (`threading.local`) instead of opening a fresh connection and re-issuing `PRAGMA journal_mode = WAL` on every request. |

### Re-verification results

**Auth (all services enforce fail-closed):**

| Service | Probe | No creds | Wrong key | Correct key |
|---------|-------|:--------:|:---------:|:-----------:|
| gateway | `GET /packs` | **401** ✅ | **401** ✅ | 200 ✅ |
| gateway | `GET /tutor/capabilities` | 401 | — | — |
| gateway | `GET /discovery` | 401 | — | — |
| inference-service | `POST /ai/content/summary` | 401 | 401 | 400 (auth passed) |
| content-pipeline | `GET /ingest/status` | 401 | 401 | 422 (auth passed) |
| pihub | `GET /classroom` | 401 | 401 | 200 |
| pack-service | `GET /packs/list` | 401 | 401 | 200 |
| experiment-service | `GET /experiments/catalog` | 401 | 401 | 200 |

**Gateway aggregate health after fix:**

```json
{"status":"healthy","checks":{"content_pipeline":{"status":"ok"},
 "inference_service":{"status":"degraded"},   // no GGUF loaded (env)
 "pack_service":{"status":"ok"},
 "experiment_service":{"healthy":true},
 "pihub":{"status":"idle"}}}
```

**Load (10×10 s) — before → after:**

| Service | RPS before | RPS after | Errors before → after | p95 before → after |
|---------|-----------:|----------:|:---------------------:|:------------------:|
| pack-service | 1772 | 1637 | 0 % → 0 % | 6.7 → 7.6 ms |
| inference-service | 1403 | 1291 | 0 % → 0 % | 8.1 → 8.9 ms |
| experiment-service | 654 | 568 | 0 % → 0 % | 18.4 → 22.0 ms |
| content-pipeline | 282 | 268 | 0 % → 0 % | 46.6 → 48.7 ms |
| **pihub** | 270 | **814** | **45 % → 0 %** | 58.2 → 16.3 ms |
| gateway | 32 | 16 | 0 % → 0 % | 647 → 734 ms |

**Stress breakpoints — before → after:**

| Service | Breaking point (C) before | after |
|---------|:-------------------------:|:-----:|
| pihub | 10 | **none (up to 50)** |
| gateway | 50 | 25 |
| content-pipeline / inference / experiment / pack | none | none |

The gateway throughput figure changed because its `/health` now performs **real,
authenticated** fan-out work (previously several downstream calls short-circuited
on 401). It remains a heavyweight aggregate endpoint — see L1.

## 11. Raw results index

| File | Contents |
|------|----------|
| `results/smoke-<svc>.json` | health status/latency + OpenAPI route list |
| `results/auth-<svc>.json` | no-creds / wrong-key / correct-key probe results |
| `results/load-<svc>.json` | 10×10 s load: RPS, error rate, latency percentiles, status counts |
| `results/stress-<svc>.json` | concurrency ramp 1→50, per-level metrics, breaking point |
