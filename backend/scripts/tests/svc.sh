#!/usr/bin/env bash
# Start/stop ONE PIHUB backend service using its existing Docker image but with
# the CURRENT source tree bind-mounted in, so we exercise today's code without
# rebuilding heavyweight images.
#
# Usage: svc.sh <start|stop|logs> <service>
# Services: gateway | content-pipeline | inference-service | pihub | pack-service | experiment-service
set -euo pipefail

BACKEND_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DOCKER=/snap/bin/docker
SHARED_VOL="${SHARED_VOL:-backend_shared_storage}"

INTERNAL_KEY="$(cat "${BACKEND_DIR}/.cp_audit.key")"
JWT_KEY="$(cat "${BACKEND_DIR}/.local_run/jwt_secret.key")"

cmd="${1:?usage: svc.sh <start|stop|logs> <service>}"
svc="${2:?missing service name}"

case "$svc" in
  gateway)            img=backend-gateway:latest;          port=8000; module="app.main:app"; src="gateway/app";          dst="/app/app"; pip_extra="pyjwt" ;;
  content-pipeline)   img=backend-content-pipeline:latest; port=8001; module="app.main:app"; src="content-pipeline/app"; dst="/app/app"; pip_extra="pyjwt pymongo" ;;
  inference-service)  img=backend-inference-service:latest;port=8010; module="app.main:app"; src="inference-service/app";dst="/app/app"; pip_extra="pyjwt" ;;
  pihub)              img=backend-pihub:latest;            port=8020; module="api.main:app"; src="pihub";              dst="/app";     pip_extra="pyjwt" ;;
  pack-service)       img=backend-pack-service:latest;     port=8030; module="app.main:app"; src="pack-service/app";    dst="/app/app"; pip_extra="pyjwt filelock" ;;
  experiment-service) img=backend-experiment-service:latest;port=8040;module="app.main:app"; src="experiment-service";  dst="/app";     pip_extra="pyjwt" ;;
  *) echo "unknown service: $svc" >&2; exit 2 ;;
esac

name="test-${svc}"

common_env=(
  -e JWT_SECRET_KEY="$JWT_KEY"
  -e INTERNAL_SERVICE_KEY="$INTERNAL_KEY"
  -e LOG_LEVEL=INFO
  -e CONTENT_PIPELINE_URL=http://localhost:8001
  -e INFERENCE_SERVICE_URL=http://localhost:8010
  -e PIHUB_URL=http://localhost:8020
  -e PACK_SERVICE_URL=http://localhost:8030
  -e EXPERIMENT_SERVICE_URL=http://localhost:8040
  -e QDRANT_URL=http://localhost:6333
  -e MONGO_URL=mongodb://localhost:27017
  -e QDRANT_COLLECTION=educational_chunks
  -e MINIO_ACCESS_KEY=test-minio-admin
  -e MINIO_SECRET_KEY=test-minio-secret-not-default
)

svc_env=()
case "$svc" in
  content-pipeline)
    svc_env=(
      -e EMBEDDING_MODEL_NAME=BAAI/bge-small-en-v1.5
      -e EMBEDDING_BACKEND=sentence_transformers
      -e EMBEDDING_FALLBACK=hash
      -e EMBEDDING_CACHE_DIR=/shared/models/sentence-transformers/embedding/BAAI_bge-small-en-v1.5
      -e LOCAL_RETRIEVAL_CACHE_DIR=/shared/models/sentence-transformers/retrieval
      -e WORK_DIR=/shared/work
      -e CONTENT_DIR=/shared/content
      -e UPLOAD_DIR=/shared/uploads
      -e MEDIA_STORAGE_PATH=/shared/media
      -e INGEST_EXTRACTOR=fitz
      -e INGEST_SKIP_IMAGES=true
      -e ENABLE_MONGO_STORAGE=false
    ) ;;
  pack-service)
    svc_env=(
      -e PACK_STORAGE_PATH=/shared/packs
      -e CURRICULUM_GRAPH_PATH=/shared/work/curriculum_graph.json
      -e PDF_LIBRARY_PATH=/shared/textbooks
      -e PDF_MANIFEST_PATH=/shared/packs/pdf_manifests/pdf_manifest.json
    ) ;;
  pihub)
    svc_env=(
      -e PIHUB_DB_PATH=/shared/pihub_test.sqlite3
      -e PIHUB_ADMIN_TOKEN=test-admin-token
      -e PIHUB_DEVICE_TOKEN_SECRET=test-device-secret
      -e PIHUB_CLASSROOM_NAME="Test Classroom"
      -e STORAGE_DIR=/shared/pihub_storage
      -e CACHE_DIR=/shared/pihub_cache
      -e LOGS_DIR=/shared/pihub_logs
      -e PACKS_DIR=/shared/pihub_packs
    ) ;;
esac

case "$cmd" in
  start)
    # Services that mount the whole /app root cannot also create /app/shared,
    # so place the shared package at /opt/shared and expose it via PYTHONPATH.
    shared_dst="/app/shared"
    py_env=()
    src_abs="${BACKEND_DIR}/${src}"
    mount_mode="ro"
    if [[ "$svc" == "pihub" || "$svc" == "experiment-service" ]]; then
      shared_dst="/opt/shared"
      py_env=(-e PYTHONPATH="/app:/opt")
    fi
    if [[ "$svc" == "experiment-service" ]]; then
      # This service writes SQLite state into its own package tree, so mount a
      # writable copy instead of the (read-only) source checkout.
      scratch="${BACKEND_DIR}/.local_run/test_src/experiment-service"
      mkdir -p "$(dirname "$scratch")"
      rm -rf "$scratch"
      cp -a "${BACKEND_DIR}/experiment-service" "$scratch"
      src_abs="$scratch"
      mount_mode="rw"
    fi
    $DOCKER rm -f "$name" >/dev/null 2>&1 || true
    $DOCKER run -d --name "$name" --network host \
      -v "${BACKEND_DIR}/shared:${shared_dst}:ro" \
      -v "${src_abs}:${dst}:${mount_mode}" \
      -v "${SHARED_VOL}:/shared" \
      "${common_env[@]}" "${svc_env[@]}" "${py_env[@]}" \
      "$img" sh -c "pip install -q ${pip_extra} >/dev/null 2>&1 || true; exec uvicorn ${module} --host 0.0.0.0 --port ${port}"
    echo "started ${name} on :${port}"
    ;;
  stop)
    $DOCKER rm -f "$name" >/dev/null 2>&1 || true
    echo "stopped ${name}"
    ;;
  logs)
    $DOCKER logs "$name" 2>&1
    ;;
  *)
    echo "usage: svc.sh <start|stop|logs> <service>" >&2; exit 2 ;;
esac
