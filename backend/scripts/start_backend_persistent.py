#!/usr/bin/env python3
import os
import sys
import time
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEXTBOOKS_ROOT = Path(os.getenv("TEXTBOOKS_ROOT", str(ROOT.parents[1] / "TEXTBOOKS")))
PY = ROOT / ".venv" / "bin" / "python"
if not PY.exists() or subprocess.run([str(PY), "-c", "import uvicorn"], capture_output=True).returncode != 0:
    PY = Path(sys.executable or "/usr/bin/python3")

LOCAL_RUN = ROOT / ".local_run"
LOCAL_RUN.mkdir(parents=True, exist_ok=True)

SERVICES = [
    ("gateway", "gateway", "app.main:app", 8000),
    ("content-pipeline", "content-pipeline", "app.main:app", 8001),
    ("inference-service", "inference-service", "app.main:app", 8010),
    ("pihub", "pihub", "api.main:app", 8020),
    ("pack-service", "pack-service", "app.main:app", 8030),
]

processes = []

env_vars = {
    **os.environ,
    "ALLOW_ANONYMOUS_DEV": "true",
    "ENVIRONMENT": "development",
    "UPLOAD_DIR": str(LOCAL_RUN / "uploads"),
    "WORK_DIR": str(LOCAL_RUN / "work"),
    "MEDIA_STORAGE_PATH": str(LOCAL_RUN / "media"),
    "PACK_STORAGE_PATH": str(LOCAL_RUN / "packs"),
    "CONTENT_DIR": str(LOCAL_RUN / "content"),
    "CURRICULUM_GRAPH_PATH": str(LOCAL_RUN / "work" / "curriculum_graph.json"),
    "CURRICULUM_RELATION_GRAPH_PATH": str(LOCAL_RUN / "work" / "curriculum_relation_graph.json"),
    "CURRICULUM_BUILD_DIR": str(LOCAL_RUN / "curriculum"),
    "CURRICULUM_MANIFEST_PATH": str(LOCAL_RUN / "curriculum" / "curriculum_manifest.json"),
    "PACKS_DIR": str(LOCAL_RUN / "pihub_packs"),
    "TEXTBOOKS_ROOT": str(TEXTBOOKS_ROOT),
    "PDF_LIBRARY_PATH": str(TEXTBOOKS_ROOT),
    "PDF_MANIFEST_PATH": str(LOCAL_RUN / "packs" / "pdf_manifests" / "pdf_manifest.json"),
    "PIHUB_DB_PATH": str(LOCAL_RUN / "pihub.sqlite3"),
    "CACHE_DIR": str(LOCAL_RUN / "pihub_cache"),
    "STORAGE_DIR": str(LOCAL_RUN / "pihub_storage"),
    "LOGS_DIR": str(LOCAL_RUN / "pihub_logs"),
    "GEMMA_MODEL_CACHE_DIR": str(LOCAL_RUN / "models" / "gemma4_audio"),
    "EMBEDDING_CACHE_DIR": str(LOCAL_RUN / "models" / "sentence-transformers" / "embedding"),
    "LOCAL_RETRIEVAL_CACHE_DIR": str(LOCAL_RUN / "models" / "sentence-transformers" / "retrieval"),
}

for name, service_dir, app_target, port in SERVICES:
    cwd = ROOT / service_dir
    service_env = {
        **env_vars,
        "PYTHONPATH": f"{ROOT}:{ROOT}/shared:{cwd}:{ROOT}"
    }
    log_file = LOCAL_RUN / "logs" / f"{name}.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    f = open(log_file, "a")
    cmd = [str(PY), "-m", "uvicorn", app_target, "--host", "0.0.0.0", "--port", str(port)]
    p = subprocess.Popen(cmd, cwd=cwd, env=service_env, stdout=f, stderr=subprocess.STDOUT)
    processes.append((name, port, p))
    print(f"Launched {name} on :{port} (PID {p.pid})")

print("All backend services launched. Entering process monitor loop...")
try:
    while True:
        time.sleep(2)
        for name, port, p in processes:
            if p.poll() is not None:
                print(f"[WARNING] Service {name} on :{port} exited with code {p.returncode}")
except KeyboardInterrupt:
    print("Shutting down services...")
    for name, port, p in processes:
        p.terminate()
