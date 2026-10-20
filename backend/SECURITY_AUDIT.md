# Backend Security Audit — Mentora / PiHub

**Date:** 2026-10-18
**Scope:** `pihub/backend` — gateway, content-pipeline, inference-service, pack-service,
pihub, experiment-service, voice-service, and the shared library.
**Method:** static enumeration of all FastAPI route decorators + auth dependencies
(`/tmp/opencode/route_audit.py`), review of `shared/security.py` and every service's auth wiring,
plus live reproduction of the anonymous-delete incident.

---

## 1. Executive summary

The backend exposed **305 HTTP endpoints, of which 244 had no authentication at all**, and
no service applied an application-wide auth guard. Because `run_backend.sh` exported
`ALLOW_ANONYMOUS_DEV=true`, even the 61 endpoints that *did* declare an auth dependency
were effectively open. This is what allowed an anonymous `POST /admin/reset-rag` to delete the
entire RAG collection.

| Severity | Count | Status after this change |
|---|---|---|
| Critical | 3 | Fixed |
| High | 4 | Fixed |
| Medium | 5 | Fixed / documented |
| Low | 4 | Documented |

**Critical / High findings are all remediated in code**; see §4 for the exact changes and §6 for
the residual recommendations.

---

## 2. Critical findings

### C1 — Anonymous destructive endpoint deleted production RAG data
`content-pipeline/app/main.py` `POST /admin/reset-rag` (and the gateway mirror) had **no auth
dependency**, and `pipeline.reset_rag_state()` **deletes and recreates the Qdrant collection**.
A single unauthenticated POST wiped all 2,642 indexed chunks. There was no backup/snapshot.

### C2 — Global fail-open: `ALLOW_ANONYMOUS_DEV` bypassed every guarded endpoint
`shared/security.py:134` inside `verify_internal_api_key`:
```python
if os.getenv("ALLOW_ANONYMOUS_DEV", "false").lower() == "true":
    return True   # ← any request, any method, any path
```
`run_backend.sh:94` exported `ALLOW_ANONYMOUS_DEV="true"`, so the dev default made the whole
backend anonymous-bypassable. The 15 gateway routes and 2 experiment-service routes that used
`verify_internal_api_key` were therefore also unprotected in practice.

### C3 — 244 endpoints with no auth (no app/router-level dependency anywhere)
No `FastAPI(...)` or `APIRouter(...)` in any service registered a global `dependencies=[...]`
guard. Coverage before the fix:

| Service | Endpoints | With route auth | Unauthenticated |
|---|---|---|---|
| gateway | 101 | 15 | **86** |
| experiment-service | 64 | 4 | **60** |
| pack-service | 29 | 0 | **29** |
| pihub | 71 | 42 | **29** |
| content-pipeline | 26 | 0 | **26** |
| inference-service | 7 | 0 | **7** |
| voice-service | 7 | 0 | **7** |
| **Total** | **305** | **61** | **244** |

Writable/unauthenticated endpoints included all of `/ingest/*`, `/upload`, `/content/upload`,
`/manifest/migrate`, and `/deployment/cache/cleanup`.

---

## 3. High findings

### H1 — No login/registration endpoint wired to the auth service
`gateway/app/services/auth_service.py` implements `register_user`, `authenticate_user`,
`create_access_token`, `decode_access_token`, `get_current_user_*` — but **no route used them**.
There was no way for a client to obtain a JWT, so the system could only be "used" by leaving it
open. (Now fixed — see §4.5.)

### H2 — Destructive operations guarded only by "any valid JWT"
Where write endpoints *were* behind `verify_internal_api_key`, any authenticated end-user token
(role `student`) could trigger ingest/upload/reset. There was no admin/role gate and no
separation between user tokens and service-to-service calls.

### H3 — Inter-service internal key is random per process when unset
`shared/security.py:63-66`:
```python
_GLOBAL_INTERNAL_SERVICE_KEY = os.getenv("INTERNAL_SERVICE_KEY")
if not _GLOBAL_INTERNAL_SERVICE_KEY or ... == "change-me":
    _GLOBAL_INTERNAL_SERVICE_KEY = secrets.token_hex(32)   # different in every process
```
If `INTERNAL_SERVICE_KEY` is not set, each service generates its own random key, so
service-to-service authentication can never succeed consistently. (Now set explicitly by the
launcher.)

### H4 — JWT signing secret fallback is per-working-directory
`_get_or_create_fallback_jwt_secret()` persists to a **relative** path `.local_run/jwt_secret.key`.
Services started from different working directories silently use different signing secrets,
so JWTs minted by the gateway fail verification downstream. (Now an explicit `JWT_SECRET_KEY`
is exported by the launcher.)

---

## 4. Remediation applied

### 4.1 `shared/security.py`
- **Removed the `ALLOW_ANONYMOUS_DEV` bypass** from `verify_internal_api_key` — a missing/invalid
  credential is now always rejected.
- Added **`InternalAuthMiddleware`**: a fail-closed, ASGI-native middleware applied to every
  service. It allows only:
  - public paths (`/health*`, `/ready*`, `/live*`, `/ping`, `/metrics*`, `/docs`, `/redoc`,
    `/openapi.json`), or
  - a valid `X-Internal-Service-Key` **or** Bearer JWT, or
  - **read-only** requests (`GET/HEAD/OPTIONS`) when `ALLOW_ANONYMOUS_DEV=true` — never
    mutations, so nothing can be created/deleted anonymously. WebSocket/lifespan scopes pass
    through untouched.
- Added **`require_admin`**: accepts the internal service key or an admin-role JWT
  (`admin`/`superadmin`/`owner`/`service`). A plain user token is rejected and the dev escape
  hatch never applies.

### 4.2 Middleware wired into every app
`app.add_middleware(InternalAuthMiddleware, …)` added in:
`content-pipeline/app/main.py`, `gateway/app/main.py`, `inference-service/app/main.py`,
`pack-service/app/main.py`, `pihub/api/main.py` (with an `extra_check` for its own
`X-PiHub-Token`/`X-Device-Token` scheme), `experiment-service/app/main.py`, `voice-service/app.py`.

### 4.3 Destructive / write endpoints upgraded to `require_admin`
- `content-pipeline`: `/ingest/pdf`, `/ingest/textbook`, `/ingest/generated-pack`,
  `/ingest/directory`, `/admin/reset-rag`.
- `gateway`: `/ingest/pdf`, `/ingest/textbook`, `/ingest/generated-pack`, `/ingest/directory`,
  `/admin/reset-rag`, `/upload`, `/content/upload`.
- `experiment-service`: `/manifest/migrate`.
- `pihub`: `/deployment/cache/cleanup` (already used pihub `require_admin`).

### 4.4 Gateway authenticates its service hops
`_proxy_request` now always injects `X-Internal-Service-Key: get_internal_service_key()` so
downstream services can stay locked down regardless of the original caller.

### 4.5 Auth flow restored
Added `POST /auth/register` and `POST /auth/login` to the gateway using the existing
`auth_service` (self-registration restricted to `student`/`teacher`/`parent` to prevent
privilege escalation; returns a signed JWT usable against the rest of the API).

### 4.6 Launcher fails closed
`run_backend.sh` no longer exports `ALLOW_ANONYMOUS_DEV=true`. It now generates and persists
`.local_run/jwt_secret.key`, `.local_run/internal_service.key`, `.local_run/minio_access.key`
and `.local_run/minio_secret.key` (mode 600) and exports `JWT_SECRET_KEY`,
`INTERNAL_SERVICE_KEY`, `MINIO_ACCESS_KEY` and `MINIO_SECRET_KEY` to every service
(fixes H3/H4, and satisfies `shared/config.py:validate_security_config`, which refuses to boot
in non-dev mode with default JWT/MinIO secrets).

---

## 5. How to use the secured backend

```bash
# 1. start (auto-creates secrets)
./run_backend.sh

# 2. register / log in to obtain a JWT
curl -s -X POST localhost:8000/auth/register \
  -H 'Content-Type: application/json' \
  -d '{"email":"a@b.com","password":"supersecret","full_name":"A","role":"student"}'
# -> { ..., "access_token": "<JWT>" }

# 3. call protected endpoints
curl -s localhost:8000/rag/search -H "Authorization: Bearer <JWT>" \
  -H 'Content-Type: application/json' -d '{"query":"photosynthesis","limit":5}'

# 4. service-to-service / admin operations require the internal key (or an admin JWT)
curl -s -X POST localhost:8001/admin/reset-rag -H "X-Internal-Service-Key: $(cat .local_run/internal_service.key)"
```
Anonymous `POST /admin/reset-rag` now returns **401**; anonymous mutating calls return **401**;
anonymous reads return **401** unless `ALLOW_ANONYMOUS_DEV=true` (read-only only).

---

## 6. Residual recommendations (not yet implemented)

1. **Qdrant backups/snapshots** before any destructive migration; add a pre-reset snapshot and a
   `?confirm=true` requirement, and audit-log who resets.
2. **Per-service keys** (mTLS or separate tokens) instead of one shared internal key; rotate.
3. **Role enforcement is coarse.** Add per-endpoint scope checks (e.g. teacher-only classroom ops)
   and require admin JWT (not the service key) for human-facing destructive actions.
4. **CORS:** `voice-service` sets `allow_origins=["*"]` together with `allow_credentials=True`
   (invalid/unsafe combination). Restrict to known origins.
5. **Rate limiting / body-size limits** on auth and ingest endpoints to blunt brute force & DoS
   (only uploads have a byte cap today).
6. **Move secrets out of dev files** into the environment/secret manager; never commit
   `.local_run/*.key`. Add them to `.gitignore`.
7. **Audit logging** for all auth failures and admin/write actions to a tamper-evident sink.
8. **JWT hardening:** consider short-lived access + refresh tokens, issuer/audience claims, and
   key IDs so the shared secret can be rotated.
9. **Automated test** asserting every route requires auth (a route added without a guard should
   fail CI). Example assertion: for each app, every non-public path returns 401 without creds.
10. **Least privilege for `run_backend.sh` dev mode** — replace the read-only dev bypass with a
    locally minted dev JWT.

---

## 7. Evidence

- Route inventory & raw results: `/tmp/opencode/route_audit.py`, `/tmp/opencode/route_audit.json`.
- Live repro (pre-fix): anonymous `POST /admin/reset-rag` → `{"status":"reset"}`, collection
  points `2642 → 0`, no credential supplied.
- Post-fix verification: see §5 (401 for anonymous mutating calls; 200 with a valid key/token).
