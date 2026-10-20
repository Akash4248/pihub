# PiHub Service Audit — `pihub/backend/pihub`

**Scope:** `api/main.py` (864) + `cache/*` (store, cache_manager, sync_engine, pack_manager) + `sync/*` + `distribution/*` + `devices/*` + `network/discovery` + `deployment/*` + `monitoring/*` + `backend/coordinator`  
**Date:** 2026-09-12

---

## 1. Duplicate Logic

| ID | Files | Finding | Fix |
|---|---|---|---|
| A1 | `cache/cache_manager.py:39` `PiCacheManager` (528L) vs `cache/manager.py:46` `CacheManager` (257L) vs `cache/pack_manager.py:8` `PackManager` + `cache/store.py:537` `store.cache_pack` | 3 pack caches (LRU, `cache_entries`, `pack_cache` SQLite) — divergent eviction/stats; `cache_manager` never instantiated in `api/main.py`. | Keep `PackManager+store.pack_cache` single source. |
| A2 | `sync/manager.py:11` + `sync/sync_engine.py:8` `SyncEngine` vs `sync/engine.py:93` second `SyncEngine` (in-memory `job_queue`) vs `cache/sync_engine.py:72` `PiSyncEngine` (httpx host delta) vs `deployment/sync_recovery.py:21` `PersistentSyncQueue`/`TransferRecoveryCoordinator` vs `backend/coordinator.py:89` `DeferredBackendSyncManager` | 5 sync engines / 3 queues (`sync_queue` table + `sync_queue_persistent.json` + `job_queue` dict) for one concern; `broadcast_to_classroom` duplicated twice. | Keep DB-backed `sync/sync_engine.SyncEngine` + merge persistence; delete others. |
| A3 | `distribution/pack_manager.py:74` `PackDistributionManager` vs `deployment/pack_hardening.py` `PackValidator/BroadcastCoordinator` vs `cache/sync_engine:276` `_download_manifest` | Parallel manifest fetch/broadcast; `list_pack_versions` in-memory vs table. | Single `PackDistributionManager` on `store.pack_versions`. |
| A4 | `api/main.py:190-246` device routes inline + `cache/store.py:257` `register_device` vs `devices/manager.py:74` `DeviceManager` (separate `active_sessions` dict) | Routes bypass `DeviceManager` — two sources of truth, groups `get_group_devices` never called. | Routes → `DeviceManager`. |
| A7 | `cache/monitoring.py:40` `MetricsCollector` vs `deployment/monitoring.py` `ClassroomMetricsCollector` vs `monitoring/health.py` `HealthMonitor` | 3 metrics layers overlapping `get_sync_metrics`. | One `MonitoringService`. |

---

## 2. Bugs — Hardcoded

| File:Line | Bug |
|---|---|
| `api/main.py:53-57` `admin_token="change-me"`, `device_token_secret="change-me-too"` | Committed defaults — fail-closed if not set. |
| `api/main.py:73` `max_cache_size_mb=500` + `cache/cache_manager:44` same `500` | Divergent defaults drift. |
| `cache/sync_engine.py:78` `host_url="http://192.168.1.100"` | Private IP vs hotspot `192.168.42.0/24` (`deployment/hotspot:92`). |
| `deployment/hotspot.py:24` `interface="wlan0"`, `26` `/etc/hostapd`, `server=8.8.8.8` | Hardcoded infra; leaks DNS in air-gap. |
| `network/discovery.py:122` `("wlan0","ap0","uap0")`, `167` `gateway "192.168.1.1"` | Magic fallback `0.0.0.0` CIDR. |

---

## 3. Bugs — Missing Validation / Security

| File:Line | Severity | Bug |
|---|---|---|
| `api/main.py:152-154` `require_admin` `x-pihub-token == admin_token` | No timing-safe compare, no lockout; many routes (`/devices/{id}/status:234-240`) leak `auth_token+device_ip` without auth. |
| `api/main.py:157-159` `SHA256(uuid+secret)` hex | Not HMAC, predictable. |
| `api/main.py:254-305` `create_pack` | `metadata=json.loads` unvalidated + `file.filename` unsanitized `Path(target)` traversal `../../.ssh` + no MIME check, trusts subject strings. |
| `api/main.py:318-325` `FileResponse(pack["file_path"])` | Arbitrary file read if traversal above succeeded; no checksum verify. |
| `api/main.py:532-538` `setup_hotspot` passphrase newline → `hostapd.conf` injection. | Newline injection. |
| `api/main.py:182-187` `list_devices` strips `auth_token` but `status:234` returns full dict with `auth_token`. | Info leak. |
| `cache/store.py:589-605` `trust_device` anyone with admin can trust any `device_id`, `is_trusted` no expiry. | Priv esc. |

---

## 4. Bugs — Race / Concurrency

| File:Line | Bug |
|---|---|
| `cache/store.py:17-24` WAL correct but 4 managers concurrent `evict_if_needed:36-48` TOCTOU. |
| `cache/cache_manager:74-86` `current_size_bytes` increment outside `RLock`; daemon thread no join. |
| `cache/manager:54-70` `cache_index.json` read/write without lock → torn write. |
| `cache/sync_engine:96-105` `if pack_id in active_downloads` check before lock → double download `"ab"`. |
| `deployment/sync_recovery:44-54` `write_text` no file lock → interleaved JSON. |

---

## 5. Bugs — Silent Fallback

| File:Line | Bug |
|---|---|
| `api/main.py:68-71` `target_dir.mkdir except pass` → later obscure `FileNotFound`. |
| `cache/manager:69` `except: pass` corrupt `cache_index.json` → drop all entries silent. |
| `sync/sync_engine:73-78` `_handle_progress_push: pass` — enqueued sync actions no-ops drop work. |
| `network/discovery:113` `except pass` → `disconnected` no reason. |

---

## 6. Overlap — Pihub Classroom vs Experiment-Service Classroom (critical)

Same word, two domains:

| Aspect | Pihub (`cache/store:42` `classroom` single row, `network/discovery:39` `ClassroomSession{network_interface,local_ip}`) | Experiment-service (`classroom_repository:11` `classroom_sessions(classroom_id,teacher_id,title)`) | Conflict |
|---|---|---|---|
| Path collision | `POST /network/session` vs `POST /classroom/sessions` vs `GET /classroom` — gateway ambiguous. | Same `ClassroomSession` class name, different fields. |
| Identity | `device_id/device_name/role` vs `student_id` — no mapping. | `ensure_student:141` creates `classroom_students` but `device_sessions` never updated. |
| Distribution | Pihub `broadcast_pack_to_classroom` binary push vs experiment-service `SharePackage` signed manifest — no bridge, teacher's PhET assignment never reaches pihub cache. | Diverged analytics (`deployment/monitoring` vs `classroom_repository.analytics:181`). |

**Fix:** Rename experiment-service to `AssignmentSession`, introduce `device_id↔student_id` map, single `classroom_id` registry via `backend/coordinator`.

---

## 7. Dead Code

`PiCacheManager` 528L, `cache/monitoring.py` 295L, `cache/retrieval_optimization.py`, `cache/debug_routes.py`, `sync/engine.py` 2nd `SyncEngine`, `cache/store:81` `cache_entries` legacy, `distribution/pack_manager:218` metrics never read, `devices/manager:188` groups never called, `deployment/validation:33` always `pending` stub, dashboard `api/main.py:723` `content_packs` property dead.

---

**Priority:** Unify cache (1), unify sync (1 DB + merged persistence), rename classroom collision + mapping, harden `file.filename` + timing-safe `require_admin` + `FileResponse` path check, fix `write_text` → temp+rename+fcntl, wire `DeviceManager` for routes.

*Evidence: 864-line `api/main.py`, 6 cache files, `Mentora` 303/321 routes unwired, `store.py` WAL `busy_timeout 10000` correct but TOCTOU.*
