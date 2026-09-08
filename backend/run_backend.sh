#!/usr/bin/env bash
# Run the full PIHUB backend natively (no docker-compose for the Python services).
# Starts Qdrant + Mongo in Docker (lightweight, required by the services),
# then launches all FastAPI services as background processes with logs.
#
# Usage:
#   ./run_backend.sh            # start everything
#   ./run_backend.sh stop       # stop everything
#   ./run_backend.sh logs       # tail all service logs
#   ./run_backend.sh status     # show running services
#
# Prereqs: backend/.venv, backend/.env, docker (for qdrant/mongo).
# For media enrichment also run cobalt + searxng (see bottom of script).

set -euo pipefail
cd "$(dirname "$0")"
ROOT="${PWD}"

LOG_DIR="${ROOT}/.local_run/logs"
PID_DIR="${ROOT}/.local_run/pids"
mkdir -p "$LOG_DIR" "$PID_DIR"

# ── Resolve Python ────────────────────────────────────────────────────────────
# Auto-create the venv if it doesn't exist, then install requirements.
VENV="${ROOT}/.venv"
if [[ ! -f "${VENV}/bin/python" ]]; then
  echo ">> .venv not found. Creating virtual environment at ${VENV} ..."
  python3 -m venv "${VENV}"
  echo ">> Installing requirements from all services..."
  "${VENV}/bin/pip" install --upgrade pip -q
  while IFS= read -r req; do
    echo "   Installing $req"
    "${VENV}/bin/pip" install -r "$req" -q
  done < <(find "${ROOT}" -maxdepth 2 -name "requirements.txt" ! -path "*/.venv/*" | sort)
  echo ">> venv ready."
fi

PY="${VENV}/bin/python"
if [[ ! -f "$PY" ]]; then
  echo "[ERROR] Could not find or create python at ${PY}. Install python3-venv and retry."
  exit 1
fi
# Local substitutes for the Docker volume mounts so native runs don't need root.
mkdir -p "$ROOT/.local_run"/{uploads,work,media,packs,textbooks,models/gemma4_audio,models/sentence-transformers/embedding,models/sentence-transformers/retrieval,content,curriculum,pihub_packs,pihub_cache,pihub_storage,pihub_logs}
mkdir -p "$ROOT/.local_run/packs/pdf_manifests"

# service name -> "dir : module:app : port"
# Each service is run from its own directory so `from app...` resolves
# (matches the Docker images where PYTHONPATH=/app and /app/app is the package).
SERVICES=(
  "gateway:gateway:app.main:app:8000"
  "content-pipeline:content-pipeline:app.main:app:8001"
  "inference-service:inference-service:app.main:app:8010"
  "pihub:pihub:api.main:app:8020"
  "pack-service:pack-service:app.main:app:8030"
  "experiment-service:experiment-service:app.main:app:8040"
)

DOCKER_STORES=(qdrant mongo)

# Start Qdrant + Mongo in Docker (non-blocking; fails soft if docker/pull is slow).
# Pass `nostores` to skip entirely (e.g. when stores run elsewhere).
start_stores() {
  if ! command -v docker >/dev/null 2>&1; then
    echo ">> docker not found; skipping stores (set QDRANT_URL/MONGO_URL in .env)"
    return 0
  fi
  echo ">> starting docker stores (qdrant, mongo) in background"
  docker rm -f pihub-qdrant pihub-mongo >/dev/null 2>&1 || true
  docker run -d --name pihub-qdrant -p 6333:6333 qdrant/qdrant:v1.13.4 >/dev/null 2>&1 &
  docker run -d --name pihub-mongo  -p 27017:27017 mongo:7.0 >/dev/null 2>&1 &
  echo "   (stores launching in background; services will retry connection)"
}

stop_stores() {
  echo ">> stopping docker stores"
  docker rm -f pihub-qdrant pihub-mongo >/dev/null 2>&1 || true
}

start_service() {
  local name="$1" dir="$2" mod="$3" appobj="$4" port="$5"
  echo ">> starting $name on :$port"
  cd "$dir"
  PYTHONPATH="$ROOT:$ROOT/shared:${PWD}:${PWD}/.." \
  UPLOAD_DIR="$ROOT/.local_run/uploads" \
  WORK_DIR="$ROOT/.local_run/work" \
  MEDIA_STORAGE_PATH="$ROOT/.local_run/media" \
  PACK_STORAGE_PATH="$ROOT/.local_run/packs" \
  CONTENT_DIR="$ROOT/.local_run/content" \
  CURRICULUM_GRAPH_PATH="$ROOT/.local_run/work/curriculum_graph.json" \
  CURRICULUM_RELATION_GRAPH_PATH="$ROOT/.local_run/work/curriculum_relation_graph.json" \
  CURRICULUM_BUILD_DIR="$ROOT/.local_run/curriculum" \
  CURRICULUM_MANIFEST_PATH="$ROOT/.local_run/curriculum/curriculum_manifest.json" \
  PACKS_DIR="$ROOT/.local_run/pihub_packs" \
  PDF_LIBRARY_PATH="$ROOT/.local_run/textbooks" \
  PDF_MANIFEST_PATH="$ROOT/.local_run/packs/pdf_manifests/pdf_manifest.json" \
  PIHUB_DB_PATH="$ROOT/.local_run/pihub.sqlite3" \
  CACHE_DIR="$ROOT/.local_run/pihub_cache" \
  STORAGE_DIR="$ROOT/.local_run/pihub_storage" \
  LOGS_DIR="$ROOT/.local_run/pihub_logs" \
  GEMMA_MODEL_CACHE_DIR="$ROOT/.local_run/models/gemma4_audio" \
  EMBEDDING_CACHE_DIR="$ROOT/.local_run/models/sentence-transformers/embedding" \
  LOCAL_RETRIEVAL_CACHE_DIR="$ROOT/.local_run/models/sentence-transformers/retrieval" \
  CONTENT_GENERATION_BACKEND="llama" \
  LLAMA_MODEL_PATH="$ROOT/inference-service/models/model.gguf" \
  CONTENT_PIPELINE_URL="http://localhost:8001" \
  INFERENCE_SERVICE_URL="http://localhost:8010" \
  PIHUB_URL="http://localhost:8020" \
  PACK_SERVICE_URL="http://localhost:8030" \
  EXPERIMENT_SERVICE_URL="http://localhost:8040" \
  QDRANT_URL="http://localhost:6333" \
  MONGO_URL="mongodb://localhost:27017" \
    setsid "$PY" -m uvicorn "${mod}:${appobj}" --host 0.0.0.0 --port "$port" \
    >"$LOG_DIR/$name.log" 2>&1 &
  pid=$!
  disown "$pid" 2>/dev/null || true
  echo $pid >"$PID_DIR/$name.pid"
  cd "$ROOT"
}

stop_service() {
  local name="$1"
  if [[ -f "$PID_DIR/$name.pid" ]]; then
    local pid; pid="$(cat "$PID_DIR/$name.pid")"
    echo ">> stopping $name (pid $pid)"
    kill "$pid" 2>/dev/null || true
    rm -f "$PID_DIR/$name.pid"
  fi
}

cmd="${1:-start}"
NO_STORES=0
case "$cmd" in
  start|nostores)
    [[ "$cmd" == "nostores" ]] && NO_STORES=1
    if [[ "$NO_STORES" -eq 0 ]]; then start_stores; else echo ">> skipping docker stores"; fi
    for s in "${SERVICES[@]}"; {
      IFS=':' read -r name dir mod appobj port <<<"$s"
      start_service "$name" "$dir" "$mod" "$appobj" "$port"
    }
    echo
    echo "All services launched. Logs in $LOG_DIR/"
    echo "Health: curl http://localhost:8000/health"
    echo "Stop:   ./run_backend.sh stop"
    ;;
  stop)
    for s in "${SERVICES[@]}"; { IFS=':' read -r name _ _ <<<"$s"; stop_service "$name"; }
    stop_stores
    echo "Stopped."
    ;;
  status)
    for s in "${SERVICES[@]}"; {
      IFS=':' read -r name dir _ _ port <<<"$s"
      if [[ -f "$PID_DIR/$name.pid" ]] && kill -0 "$(cat "$PID_DIR/$name.pid")" 2>/dev/null; then
        printf "  %-20s :%s  RUNNING (pid %s)\n" "$name" "$port" "$(cat "$PID_DIR/$name.pid")"
      else
        printf "  %-20s :%s  stopped\n" "$name" "$port"
      fi
    }
    ;;
  logs)
    exec tail -f "$LOG_DIR"/*.log
    ;;
  *)
    echo "Usage: $0 {start|stop|status|logs}"
    exit 1
    ;;
esac
