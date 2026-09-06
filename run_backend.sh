#!/usr/bin/env bash
# ==============================================================================
# Mentora / PiHub Native Backend Launcher (No Docker Required)
# ==============================================================================
# Usage:
#   ./run_backend.sh           # Start all backend services natively
#   ./run_backend.sh stop      # Stop all running backend services
#   ./run_backend.sh status    # Check status of running backend services
#   ./run_backend.sh logs      # Tail live backend service logs
#   ./run_backend.sh restart   # Restart backend services
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="${SCRIPT_DIR}"
BACKEND_DIR="${ROOT_DIR}/backend"

# Determine Python Executable (.venv or system python3)
if [[ -f "${BACKEND_DIR}/.venv/bin/python" ]]; then
    PY="${BACKEND_DIR}/.venv/bin/python"
elif [[ -f "${ROOT_DIR}/.venv/bin/python" ]]; then
    PY="${ROOT_DIR}/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
    PY="$(command -v python3)"
else
    echo "[ERROR] Python 3 not found. Please install python3."
    exit 1
fi

LOG_DIR="${ROOT_DIR}/.local_run/logs"
PID_DIR="${ROOT_DIR}/.local_run/pids"
mkdir -p "${LOG_DIR}" "${PID_DIR}"

# Create storage directories
mkdir -p "${ROOT_DIR}/.local_run"/{uploads,work,media,packs,textbooks,content,curriculum,pihub_packs,pihub_cache,pihub_storage,pihub_logs}
mkdir -p "${ROOT_DIR}/.local_run/packs/pdf_manifests"

# Defined microservices -> "name:dir:module:app_object:port"
SERVICES=(
  "gateway:gateway:app.main:app:8000"
  "content-pipeline:content-pipeline:app.main:app:8001"
  "inference-service:inference-service:app.main:app:8010"
  "pihub:pihub:api.main:app:8020"
  "pack-service:pack-service:app.main:app:8030"
  "experiment-service:experiment-service:app.main:app:8040"
)

check_ollama() {
    if curl -s http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then
        echo "   [Ollama] Ollama server detected on http://127.0.0.1:11434 (Ready for Gemma-4 / AI Tutor)"
    else
        echo "   [Ollama] Warning: Ollama server not detected on http://127.0.0.1:11434. Run 'ollama serve' for backend Ollama LLM support."
    fi
}

start_service() {
    local name="$1" dir="$2" mod="$3" appobj="$4" port="$5"
    local pid_file="${PID_DIR}/${name}.pid"
    local log_file="${LOG_DIR}/${name}.log"

    if [[ -f "${pid_file}" ]] && kill -0 "$(cat "${pid_file}")" 2>/dev/null; then
        echo "   [SKIP] ${name} is already running on :${port} (PID $(cat "${pid_file}"))"
        return 0
    fi

    echo ">> Starting ${name} on http://0.0.0.0:${port}..."
    local target_dir="${BACKEND_DIR}/${dir}"
    if [[ ! -d "${target_dir}" ]]; then
        target_dir="${BACKEND_DIR}"
    fi

    PYTHONUNBUFFERED=1 \
    PYTHONPATH="${BACKEND_DIR}:${BACKEND_DIR}/shared:${target_dir}" \
    UPLOAD_DIR="${ROOT_DIR}/.local_run/uploads" \
    WORK_DIR="${ROOT_DIR}/.local_run/work" \
    MEDIA_STORAGE_PATH="${ROOT_DIR}/.local_run/media" \
    PACK_STORAGE_PATH="${ROOT_DIR}/.local_run/packs" \
    CONTENT_DIR="${ROOT_DIR}/.local_run/content" \
    PIHUB_DB_PATH="${ROOT_DIR}/.local_run/pihub.sqlite3" \
    CACHE_DIR="${ROOT_DIR}/.local_run/pihub_cache" \
    STORAGE_DIR="${ROOT_DIR}/.local_run/pihub_storage" \
    LOGS_DIR="${ROOT_DIR}/.local_run/pihub_logs" \
      setsid "${PY}" -m uvicorn "${mod}:${appobj}" --app-dir "${target_dir}" --host 0.0.0.0 --port "${port}" >"${log_file}" 2>&1 &

    local pid=$!
    echo "${pid}" >"${pid_file}"
}

stop_service() {
    local name="$1"
    local pid_file="${PID_DIR}/${name}.pid"
    if [[ -f "${pid_file}" ]]; then
        local pid; pid="$(cat "${pid_file}")"
        echo ">> Stopping ${name} (PID ${pid})..."
        kill "${pid}" 2>/dev/null || true
        rm -f "${pid_file}"
    fi
}

cmd="${1:-start}"

case "${cmd}" in
    start)
        echo "=========================================================="
        echo " Starting Mentora / PiHub Backend (Native Python / Uvicorn)"
        echo " Using Python: ${PY}"
        echo "=========================================================="
        check_ollama
        echo
        for s in "${SERVICES[@]}"; do
            IFS=':' read -r name dir mod appobj port <<<"${s}"
            start_service "${name}" "${dir}" "${mod}" "${appobj}" "${port}"
        done
        echo
        echo "=========================================================="
        echo " All backend services launched successfully!"
        echo " Gateway URL: http://0.0.0.0:8000"
        echo " Health Check: curl http://localhost:8000/health"
        echo " Logs Directory: ${LOG_DIR}/"
        echo " Commands: ./run_backend.sh {stop|status|logs|restart}"
        echo "=========================================================="
        ;;
    stop)
        echo ">> Stopping all backend services..."
        for s in "${SERVICES[@]}"; do
            IFS=':' read -r name _ _ _ _ <<<"${s}"
            stop_service "${name}"
        done
        echo "All backend services stopped."
        ;;
    status)
        echo "=========================================================="
        echo " PiHub Backend Service Status"
        echo "=========================================================="
        for s in "${SERVICES[@]}"; do
            IFS=':' read -r name dir _ _ port <<<"${s}"
            pid_file="${PID_DIR}/${name}.pid"
            if [[ -f "${pid_file}" ]] && kill -0 "$(cat "${pid_file}")" 2>/dev/null; then
                printf "  %-20s :%-5s RUNNING (PID %s)\n" "${name}" "${port}" "$(cat "${pid_file}")"
            else
                printf "  %-20s :%-5s STOPPED\n" "${name}" "${port}"
            fi
        done
        echo "=========================================================="
        ;;
    logs)
        echo ">> Tailing all service logs in ${LOG_DIR}/ (Ctrl+C to exit)..."
        exec tail -f "${LOG_DIR}"/*.log
        ;;
    restart)
        $0 stop
        sleep 1
        $0 start
        ;;
    *)
        echo "Usage: $0 {start|stop|status|logs|restart}"
        exit 1
        ;;
esac
