# PIHUB Backend — Testing Documentation

This folder contains the results of the end-to-end backend test campaign run on
**2026-10-19** against the PIHUB FastAPI services.

| Document | Description |
|----------|-------------|
| [`TEST_REPORT-2026-10-19.md`](./TEST_REPORT-2026-10-19.md) | Full report: smoke, auth/integration, load and stress results + findings. |
| [`results/`](./results) | Raw JSON output from the harness (one file per service per test kind). |

## TL;DR

* **6/6 services boot and answer health checks** (gateway, content-pipeline,
  inference-service, pihub, pack-service, experiment-service).
* **Security:** all 6 services now enforce fail-closed auth. The campaign found
  and **fixed** a critical gateway auth bypass (**B1**), a health-endpoint auth
  gap (**H1**) and an unauthenticated health fan-out (**H2**).
* **Performance (single-process uvicorn, 16 vCPU host, after fixes):**
  pack-service ~1.6k req/s, inference-service ~1.3k, pihub ~0.8k,
  experiment-service ~0.57k, content-pipeline ~0.27k, gateway ~0.02k
  (its `/health` fan-out to every downstream).
* **Stability:** all services 0 % errors under 50× concurrency except the
  gateway aggregate `/health` (p95 crosses 2 s at 25×, Finding L1). pihub's
  pre-existing 500s under concurrency (**M1**) are fixed.

## How to reproduce

```bash
cd backend

# 1. dependency stores (reuse the existing data volumes)
/snap/bin/docker run -d --name test-qdrant --network host \
  -v backend_qdrant_data:/qdrant/storage qdrant/qdrant:v1.13.4
/snap/bin/docker run -d --name test-mongo --network host \
  -v backend_mongo_data:/data/db mongo:7.0

# 2. start a service from the CURRENT source (mounted into its existing image)
./scripts/tests/svc.sh start gateway          # or any service name
./scripts/tests/svc.sh logs  gateway
./scripts/tests/svc.sh stop  gateway

# 3. run the harness (standard library only — no pip installs needed)
KEY=$(cat .cp_audit.key)
python3 scripts/tests/harness.py --name gateway --key "$KEY" smoke  --base http://localhost:8000
python3 scripts/tests/harness.py --name gateway --key "$KEY" auth   --base http://localhost:8000
python3 scripts/tests/harness.py --name gateway --key "$KEY" load   --base http://localhost:8000 --path /health --concurrency 10 --duration 10
python3 scripts/tests/harness.py --name gateway --key "$KEY" stress --base http://localhost:8000 --path /health --levels 1,5,10,25,50 --duration 5
```

Helper scripts:

* `backend/scripts/tests/svc.sh` — start/stop/log a single service using its
  existing image + the current source tree.
* `backend/scripts/tests/harness.py` — `smoke` / `auth` / `load` / `stress`
  runner that writes a JSON report.
