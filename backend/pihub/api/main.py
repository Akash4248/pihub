from __future__ import annotations

import hashlib
import json
import logging
import os
import uuid
import time
import zipfile
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse

from api.schemas import (
    ClassroomUpdateRequest,
    DeviceRegisterRequest,
    DeviceResponse,
    HealthResponse,
    ProgressRequest,
    QuizAnswerRequest,
    QuizSessionCreateRequest,
    SyncRequest,
)
from backend.coordinator import BackendCoordinator
from cache.pack_manager import PackManager
from cache.store import PiHubStore
from devices.manager import DeviceManager
from distribution.pack_manager import PackDistributionManager
from monitoring.health import HealthMonitor
from network.discovery import ClassroomIntranet, NetworkDiscovery
from shared.security import read_file_chunks_safely, InternalAuthMiddleware
from sync.manager import build_sync_manifest
from sync.sync_engine import SyncEngine
from deployment.hotspot import HotspotNetworking
from deployment.startup import DeploymentAutomation
from deployment.sync_recovery import PersistentSyncQueue, TransferRecoveryCoordinator, SyncDiagnosticsService
from deployment.load_manager import ClassroomLoadManager
from deployment.pack_hardening import PackValidator, BroadcastCoordinator, IncrementalDistributionManager
from deployment.cache_hardening import CacheValidator, CacheCleanupService, StorageDiagnosticsManager
from deployment.backend_recovery import DeferredBackendSyncManager, BackendRecoveryCoordinator, PartialSyncRecoveryService
from deployment.monitoring import ClassroomMetricsCollector, SyncMetricsService, NetworkDiagnosticsManager
from deployment.validation import ClassroomValidator, TransferStressTest, RecoveryScenarios


logger = logging.getLogger(__name__)
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))


class Settings:
    def __init__(self) -> None:
        local_base = Path(__file__).resolve().parents[2] / ".local_run"
        self.db_path = os.getenv("PIHUB_DB_PATH") or str(local_base / "pihub.sqlite3")
        self.admin_token = os.getenv("PIHUB_ADMIN_TOKEN", "change-me")
        self.device_token_secret = os.getenv("PIHUB_DEVICE_TOKEN_SECRET", "change-me-too")
        self.classroom_name = os.getenv("PIHUB_CLASSROOM_NAME", "Classroom A")
        self.backend_url = os.getenv("BACKEND_URL", "http://gateway:8000")
        self.base_dir = Path("/")
        # Fall back to local writable paths if default Docker mounts (/packs, /cache, etc.) are un-writable.
        self.packs_dir = Path(os.getenv("PACKS_DIR") or (local_base / "pihub_packs"))
        self.cache_dir = Path(os.getenv("CACHE_DIR") or (local_base / "pihub_cache"))
        self.storage_dir = Path(os.getenv("STORAGE_DIR") or (local_base / "pihub_storage"))
        self.logs_dir = Path(os.getenv("LOGS_DIR") or (local_base / "pihub_logs"))


import hmac

settings = Settings()
# Fail-closed by default (run_backend.sh exports ALLOW_ANONYMOUS_DEV=true
# for local dev, matching shared.config).
ALLOW_ANONYMOUS_DEV = os.getenv("ALLOW_ANONYMOUS_DEV", "false").lower() == "true"
if not ALLOW_ANONYMOUS_DEV:
    if settings.admin_token in ("change-me", "") or settings.device_token_secret in ("change-me-too", ""):
        raise ValueError("Production deployment requires non-default PIHUB_ADMIN_TOKEN and PIHUB_DEVICE_TOKEN_SECRET")

store = PiHubStore(settings.db_path)
for target_dir in (settings.packs_dir, settings.cache_dir, settings.storage_dir, settings.logs_dir):
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

pack_manager = PackManager(store, settings.cache_dir, max_cache_size_mb=500)
pack_distribution = PackDistributionManager(store, settings.storage_dir)
sync_engine = SyncEngine(store)
health_monitor = HealthMonitor(store)
backend_coordinator = BackendCoordinator(store, settings.backend_url)
network_discovery = NetworkDiscovery(settings.storage_dir / "network")
classroom_intranet = ClassroomIntranet(network_discovery)
device_manager = DeviceManager(store)

# Deployment Validation Phase Services
hotspot_network = HotspotNetworking(interface="wlan0", config_dir=settings.storage_dir / "network")
deployment_automation = DeploymentAutomation()
persistent_sync_queue = PersistentSyncQueue(settings.storage_dir / "sync_queue_persistent.json")
transfer_recovery = TransferRecoveryCoordinator(settings.storage_dir / "transfer_recovery.json")
sync_diagnostics = SyncDiagnosticsService(persistent_sync_queue, transfer_recovery)
classroom_load_manager = ClassroomLoadManager(max_concurrent_transfers=3)
pack_validator = PackValidator()
broadcast_coordinator = BroadcastCoordinator()
incremental_distribution = IncrementalDistributionManager()
cache_validator = CacheValidator()
cache_cleanup = CacheCleanupService(settings.cache_dir)
storage_diagnostics = StorageDiagnosticsManager(settings.cache_dir)
deferred_sync_manager = DeferredBackendSyncManager()
backend_recovery = BackendRecoveryCoordinator(deferred_sync_manager)
partial_sync_recovery = PartialSyncRecoveryService()
metrics_collector = ClassroomMetricsCollector()
sync_metrics = SyncMetricsService()
network_diagnostics = NetworkDiagnosticsManager()
classroom_validator = ClassroomValidator()
transfer_stress_test = TransferStressTest()
recovery_scenarios = RecoveryScenarios()

app = FastAPI(title="pihub")
# pihub uses its own admin/device token scheme; accept those in addition to the
# shared internal key / JWT so edge-device flows keep working while anonymous
# access is still rejected.
app.add_middleware(
    InternalAuthMiddleware,
    extra_check=lambda h: (
        bool(h.get("x-pihub-token")) and hmac.compare_digest(h["x-pihub-token"], settings.admin_token)
    ) or bool(h.get("x-device-token")),
)


def _log_tag(path: str) -> str:
    if path.startswith("/sync"):
        return "SYNC"
    if path.startswith("/packs"):
        return "PACK"
    if path.startswith("/progress"):
        return "PROGRESS"
    if path.startswith("/quiz-sessions"):
        return "PROGRESS"
    if path.startswith("/network") or path.startswith("/devices") or path.startswith("/classroom"):
        return "DISCOVERY"
    return "REQUEST"


@app.middleware("http")
async def structured_logging(request: Request, call_next):
    started = time.perf_counter()
    tag = _log_tag(request.url.path)
    logger.info("[%s] REQUEST_START method=%s path=%s", tag, request.method, request.url.path)
    try:
        response = await call_next(request)
    except Exception as exc:
        duration_ms = (time.perf_counter() - started) * 1000
        logger.exception(
            "[%s] REQUEST_ERROR method=%s path=%s duration_ms=%.2f error=%s",
            tag,
            request.method,
            request.url.path,
            duration_ms,
            exc,
        )
        raise
    duration_ms = (time.perf_counter() - started) * 1000
    logger.info(
        "[%s] REQUEST_END method=%s path=%s status=%s duration_ms=%.2f",
        tag,
        request.method,
        request.url.path,
        response.status_code,
        duration_ms,
    )
    return response


def require_admin(x_pihub_token: str | None = Header(default=None)) -> None:
    if not x_pihub_token or not hmac.compare_digest(x_pihub_token.encode("utf-8"), settings.admin_token.encode("utf-8")):
        raise HTTPException(status_code=401, detail="Admin token required")


def issue_device_token(seed: str | None = None) -> str:
    seed_str = seed or str(uuid.uuid4())
    return hmac.new(
        settings.device_token_secret.encode("utf-8"),
        seed_str.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    system_health = health_monitor.get_system_health()
    return HealthResponse(status=system_health["status"], service="pihub", checks=system_health)


@app.get("/classroom")
def get_classroom() -> dict[str, Any]:
    classroom = store.get_classroom()
    classroom.setdefault("discovery_token", issue_device_token(classroom.get("classroom_name") or settings.classroom_name))
    classroom["devices_online"] = len([d for d in store.list_devices() if d["status"] == "online"])
    classroom["network"] = network_discovery.get_network_status()
    return classroom


@app.post("/classroom")
def update_classroom(payload: ClassroomUpdateRequest, _: None = Depends(require_admin)) -> dict[str, Any]:
    return store.upsert_classroom(payload.classroom_name, payload.teacher_name, payload.sync_mode, payload.metadata)


@app.get("/devices")
def list_devices() -> dict[str, Any]:
    devices = []
    for device in store.list_devices():
        devices.append({key: value for key, value in device.items() if key not in ("auth_token", "auth_token_hash")})
    return {"devices": devices}


@app.post("/devices", response_model=DeviceResponse)
def register_device(payload: DeviceRegisterRequest) -> DeviceResponse:
    device_id, token, _ = device_manager.register_device(
        classroom_id=payload.classroom or "default",
        device_name=payload.device_name,
        device_ip=payload.metadata.get("device_ip", "0.0.0.0") if payload.metadata else "0.0.0.0",
        role=payload.role,
        metadata=payload.metadata,
    )
    device = store.get_device(device_id)
    classroom_intranet.announce_device(device_id, payload.device_name, device.get("metadata", {}).get("device_ip", "0.0.0.0"), 8000)
    # The plaintext token is shown exactly once here from the fresh secret —
    # never read back from storage (only hashes live in the DB).
    return DeviceResponse(**{**device, "auth_token": token})


@app.post("/devices/{device_id}/heartbeat")
def device_heartbeat(device_id: str, x_device_token: str | None = Header(default=None)) -> dict[str, Any]:
    if not x_device_token or not device_manager.heartbeat(device_id, x_device_token):
        raise HTTPException(status_code=401, detail="Device token required")
    return {"status": "ok", "device_id": device_id}


@app.post("/devices/{device_id}/reconnect")
def reconnect_device(device_id: str, x_device_token: str | None = Header(default=None)) -> dict[str, Any]:
    if not x_device_token:
        raise HTTPException(status_code=401, detail="Device token required")
    res = device_manager.handle_reconnect(device_id, x_device_token)
    if res.get("status") == "error":
        raise HTTPException(status_code=401, detail=res.get("message", "Device token required"))
    device = store.get_device(device_id) or {}
    classroom_intranet.announce_device(device_id, device.get("device_name", device_id), device.get("metadata", {}).get("device_ip", "0.0.0.0"), 8000)
    return {"status": "reconnected", "device_id": device_id}


@app.get("/devices/{device_id}/status")
def device_status(device_id: str) -> dict[str, Any]:
    status_data = device_manager.get_device_status(device_id)
    if status_data.get("status") == "unknown":
        raise HTTPException(status_code=404, detail="Device not found")
    return status_data


@app.post("/devices/{device_id}/trust")
def trust_device(device_id: str, _: None = Depends(require_admin)) -> dict[str, Any]:
    store.trust_device(device_id, True, 1)
    return {"device_id": device_id, "trusted": True}


@app.get("/packs")
def list_packs() -> dict[str, Any]:
    return {"packs": store.list_packs(), "cached": pack_manager.list_cached_packs()}


# Packs are legitimately large: cap streaming at 1 GiB (enforced DURING the
# upload so a bigger body can never sit in RAM), not after a full read.
PACK_MAX_BYTES = 1024 * 1024 * 1024


@app.post("/packs")
async def create_pack(
    file: UploadFile = File(...),
    pack_name: str = Form(default=""),
    version: str = Form(default="1.0.0"),
    subject: str | None = Form(default=None),
    grade: int | None = Form(default=None),
    chapter: str | None = Form(default=None),
    metadata: str = Form(default="{}"),
    _: None = Depends(require_admin),
) -> dict[str, Any]:
    try:
        metadata_dict = json.loads(metadata)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"Invalid metadata JSON: {exc}") from exc
    safe_filename = Path(file.filename or f"{pack_name or 'pack'}.zip").name
    if not safe_filename.lower().endswith((".zip", ".pack")):
        raise HTTPException(status_code=400, detail="Packs must be packaged as .zip or .pack files")
    target_path = (settings.packs_dir / safe_filename).resolve()
    packs_dir_res = settings.packs_dir.resolve()
    if not target_path.is_relative_to(packs_dir_res):
        raise HTTPException(status_code=400, detail="Invalid filename: path traversal attempt")
    # Stream straight to a temp sibling (never full body in RAM), then validate.
    tmp_path = target_path.with_name(target_path.name + ".part")
    digest = hashlib.sha256()
    size_bytes = 0
    try:
        with open(tmp_path, "wb") as sink:
            async for chunk in read_file_chunks_safely(file, max_bytes=PACK_MAX_BYTES):
                sink.write(chunk)
                digest.update(chunk)
                size_bytes += len(chunk)
            sink.flush()
            os.fsync(sink.fileno())
    except HTTPException:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    if safe_filename.lower().endswith(".zip") and not zipfile.is_zipfile(tmp_path):
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise HTTPException(status_code=400, detail="Invalid zip pack file")
    os.replace(tmp_path, target_path)
    pack_record = store.add_pack(
        {
            "pack_name": pack_name or safe_filename,
            "version": version,
            "subject": subject,
            "grade": grade,
            "chapter": chapter,
            "file_path": str(target_path),
            "checksum": digest.hexdigest(),
            "size_bytes": size_bytes,
            "metadata": metadata_dict,
        }
    )
    pack_manager.cache_pack(
        pack_record["pack_id"],
        pack_record["pack_name"],
        pack_record["version"],
        str(target_path),
        pack_record["checksum"],
        size_bytes,
        subject,
        grade,
        chapter,
    )
    pack_distribution.create_pack_manifest(
        pack_record["pack_id"],
        pack_record["pack_name"],
        pack_record["version"],
        pack_record["size_bytes"],
        pack_record["checksum"],
        subject,
        grade,
        chapter,
        pack_record.get("metadata", {}),
    )
    return pack_record


@app.get("/packs/{pack_id}")
def get_pack(pack_id: str) -> dict[str, Any]:
    pack = store.get_cached_pack(pack_id) or store.get_pack(pack_id)
    if pack is None:
        raise HTTPException(status_code=404, detail="Pack not found")
    store.touch_pack(pack_id)
    pack_manager.touch_cached_pack(pack_id)
    return pack


@app.get("/packs/{pack_id}/download")
def download_pack(pack_id: str) -> FileResponse:
    pack = store.get_cached_pack(pack_id) or store.get_pack(pack_id)
    if pack is None:
        raise HTTPException(status_code=404, detail="Pack not found")
    target_path = Path(pack["file_path"]).resolve()
    if not target_path.exists():
        raise HTTPException(status_code=404, detail="Pack file missing")
    packs_dir_res = settings.packs_dir.resolve()
    cache_dir_res = settings.cache_dir.resolve()
    if not target_path.is_relative_to(packs_dir_res) and not target_path.is_relative_to(cache_dir_res):
        raise HTTPException(status_code=403, detail="Forbidden file path")
    store.touch_pack(pack_id)
    pack_manager.touch_cached_pack(pack_id)
    return FileResponse(str(target_path), filename=target_path.name)


@app.post("/sync")
def sync(payload: SyncRequest) -> dict[str, Any]:
    if payload.action == "start":
        session = store.start_session(
            {
                "device_id": payload.device_id,
                "resource_type": payload.resource_type,
                "resource_id": payload.resource_id,
                "offset_bytes": 0,
                "total_bytes": payload.total_bytes,
                "status": "pending",
                "checksum": payload.checksum,
                "metadata": payload.metadata,
            }
        )
        return {"session": session, "manifest": build_sync_manifest(session)}

    if payload.session_id is None:
        raise HTTPException(status_code=400, detail="session_id is required")

    if payload.action == "status":
        session = store.get_session(payload.session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="Sync session not found")
        return {"session": session, "manifest": build_sync_manifest(session)}

    if payload.action == "advance":
        session = store.update_session(
            payload.session_id,
            offset_bytes=payload.bytes_transferred or 0,
            status="transferring",
            checksum=payload.checksum,
            metadata=payload.metadata,
        )
        if session is None:
            raise HTTPException(status_code=404, detail="Sync session not found")
        return {"session": session, "manifest": build_sync_manifest(session)}

    if payload.action == "complete":
        session = store.update_session(payload.session_id, status="complete", metadata=payload.metadata)
        if session is None:
            raise HTTPException(status_code=404, detail="Sync session not found")
        return {"session": session, "manifest": build_sync_manifest(session)}

    if payload.action == "retry":
        session = store.increment_retry(payload.session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="Sync session not found")
        return {"session": session, "manifest": build_sync_manifest(session)}

    raise HTTPException(status_code=400, detail="Unsupported sync action")


@app.get("/sync")
def sync_status() -> dict[str, Any]:
    sessions = [build_sync_manifest(session) for session in store.list_sync_queue()]
    return {"sessions": sessions, "status": sync_engine.get_sync_status()}


@app.post("/progress")
def record_progress(payload: ProgressRequest) -> dict[str, Any]:
    progress = store.upsert_progress(payload.model_dump())
    return {"status": "ok", "progress": progress}


@app.get("/progress/{student_id}")
def get_progress(student_id: str) -> dict[str, Any]:
    return {"student_id": student_id, "progress": store.list_progress(student_id)}


@app.post("/quiz-sessions")
def create_quiz_session(payload: QuizSessionCreateRequest) -> dict[str, Any]:
    session = store.create_quiz_session(payload.model_dump())
    return {"status": "ok", "session": session}


@app.get("/quiz-sessions/student/{student_id}")
def list_student_quiz_sessions(student_id: str, active_only: bool = False) -> dict[str, Any]:
    if active_only:
        session = store.active_quiz_session(student_id)
        return {"student_id": student_id, "sessions": [session] if session else []}
    return {"student_id": student_id, "sessions": store.list_quiz_sessions(student_id)}


@app.get("/quiz-sessions/{quiz_session_id}")
def get_quiz_session(quiz_session_id: str) -> dict[str, Any]:
    session = store.get_quiz_session(quiz_session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Quiz session not found")
    return {"session": session}


@app.post("/quiz-sessions/{quiz_session_id}/answer")
def answer_quiz_session(quiz_session_id: str, payload: QuizAnswerRequest) -> dict[str, Any]:
    session = store.advance_quiz_session(quiz_session_id, payload.model_dump())
    if session is None:
        raise HTTPException(status_code=404, detail="Quiz session not found")
    return {"status": "ok", "session": session}


@app.post("/sync/process")
def process_sync_queue(_: None = Depends(require_admin)) -> dict[str, Any]:
    return sync_engine.process_pending_syncs()


@app.post("/sync/{session_id}/retry")
def retry_sync(session_id: str, _: None = Depends(require_admin)) -> dict[str, Any]:
    session = store.increment_retry(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Sync session not found")
    return {"session": session, "manifest": build_sync_manifest(session)}


@app.get("/cache/stats")
def cache_stats() -> dict[str, Any]:
    return pack_manager.get_cache_stats()


@app.get("/cache/health")
def cache_health() -> dict[str, Any]:
    return {"cache": pack_manager.get_cache_stats(), "backend": backend_coordinator.store.get_backend_sync_state()}


@app.get("/sessions")
def list_sessions() -> dict[str, Any]:
    return {"sessions": store.list_device_sessions()}


@app.get("/sessions/{session_id}")
def get_session(session_id: str) -> dict[str, Any]:
    session = store.get_device_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return session


@app.get("/network/status")
def network_status() -> dict[str, Any]:
    return network_discovery.get_network_status()


@app.post("/network/session")
def create_network_session(classroom_name: str | None = None, hotspot_enabled: bool = False, _: None = Depends(require_admin)) -> dict[str, Any]:
    session = network_discovery.create_classroom_session(classroom_name or settings.classroom_name, hotspot_enabled=hotspot_enabled)
    return {"session": session.__dict__ if hasattr(session, "__dict__") else session}


@app.get("/network/devices")
def discover_network_devices() -> dict[str, Any]:
    return {"devices": classroom_intranet.discover_local_devices()}


@app.post("/backend/sync")
async def backend_sync(_: None = Depends(require_admin)) -> dict[str, Any]:
    return await backend_coordinator.sync_classroom_metadata()


@app.post("/backend/sync-now")
async def backend_sync_now(_: None = Depends(require_admin)) -> dict[str, Any]:
    return await backend_coordinator.sync_classroom_metadata()


@app.get("/backend/status")
def backend_status() -> dict[str, Any]:
    return backend_coordinator.store.get_backend_sync_state()


@app.get("/backend/health")
async def backend_health(_: None = Depends(require_admin)) -> dict[str, Any]:
    available = await backend_coordinator.check_backend_health()
    return {"backend_available": available, "status": "ok" if available else "backend_unavailable"}


@app.get("/health/resources")
def health_resources(_: None = Depends(require_admin)) -> dict[str, Any]:
    return health_monitor.get_resource_usage()


@app.get("/health/diagnostics")
def health_diagnostics() -> dict[str, Any]:
    return {"system": health_monitor.get_system_health(), "backend": backend_coordinator.store.get_backend_sync_state()}


@app.post("/broadcast/pack/{pack_id}")
def broadcast_pack(pack_id: str, classroom: str | None = None, _: None = Depends(require_admin)) -> dict[str, Any]:
    queue_id = sync_engine.broadcast_to_classroom("distribute_pack", "pack", pack_id, classroom)
    manifest = pack_distribution.broadcast_pack_to_classroom(pack_id, classroom or settings.classroom_name)
    return {"queue_id": queue_id, "manifest": manifest, "status": "queued"}


@app.get("/packs/{pack_id}/distribution")
def pack_distribution_status(pack_id: str) -> dict[str, Any]:
    versions = pack_distribution.list_pack_versions(pack_id)
    cached = pack_manager.get_cached_pack(pack_id)
    return {"pack_id": pack_id, "versions": versions, "cached": cached}


# Deployment Validation Phase 1-2: Hotspot & Startup
@app.get("/deployment/hotspot/health")
def hotspot_health(_: None = Depends(require_admin)) -> dict[str, Any]:
    return hotspot_network.check_hotspot_health()


@app.post("/deployment/hotspot/setup")
def setup_hotspot(
    ssid: str,
    passphrase: str,
    channel: int = 6,
    _: None = Depends(require_admin),
) -> dict[str, Any]:
    return hotspot_network.setup_hotspot(ssid, passphrase, channel=channel)


@app.post("/deployment/hotspot/recover")
def recover_hotspot(_: None = Depends(require_admin)) -> dict[str, Any]:
    return hotspot_network.recover_hotspot()


@app.get("/deployment/hotspot/devices")
def hotspot_devices(_: None = Depends(require_admin)) -> dict[str, Any]:
    return hotspot_network.get_connected_devices()


@app.get("/deployment/startup/validate")
def validate_startup(_: None = Depends(require_admin)) -> dict[str, Any]:
    return deployment_automation.validate_deployment()


@app.get("/deployment/startup/containers")
def startup_containers(_: None = Depends(require_admin)) -> dict[str, Any]:
    return deployment_automation.ensure_all_containers_running()


@app.post("/deployment/startup/containers/{container_name}/restart")
def restart_container(container_name: str, _: None = Depends(require_admin)) -> dict[str, Any]:
    return deployment_automation.docker_recovery.restart_container(container_name)


@app.get("/deployment/status")
def deployment_status(_: None = Depends(require_admin)) -> dict[str, Any]:
    return deployment_automation.get_deployment_status()


# Deployment Validation Phase 3: Synchronization Hardening
@app.get("/deployment/sync/health")
def sync_health(_: None = Depends(require_admin)) -> dict[str, Any]:
    return sync_diagnostics.get_sync_health()


@app.get("/deployment/sync/diagnostics")
def sync_diagnostics_endpoint(_: None = Depends(require_admin)) -> dict[str, Any]:
    return sync_diagnostics.get_sync_diagnostics()


@app.get("/deployment/sync/interrupted")
def interrupted_transfers(_: None = Depends(require_admin)) -> dict[str, Any]:
    return {"interrupted_transfers": transfer_recovery.get_interrupted_transfers()}


@app.get("/deployment/sync/queue/size")
def sync_queue_size(_: None = Depends(require_admin)) -> dict[str, Any]:
    return {"queue_size": persistent_sync_queue.get_size()}


# Deployment Validation Phase 4: Classroom Load Management
@app.get("/deployment/load/status")
def load_status(_: None = Depends(require_admin)) -> dict[str, Any]:
    return classroom_load_manager.get_classroom_load()


@app.get("/deployment/load/statistics")
def load_statistics(_: None = Depends(require_admin)) -> dict[str, Any]:
    return classroom_load_manager.get_load_statistics()


@app.post("/deployment/load/record-snapshot")
def record_load_snapshot(_: None = Depends(require_admin)) -> dict[str, Any]:
    classroom_load_manager.record_load_snapshot()
    return {"status": "snapshot_recorded"}


# Deployment Validation Phase 5: Pack Hardening
@app.post("/deployment/packs/validate/{pack_id}")
def validate_pack(pack_id: str, _: None = Depends(require_admin)) -> dict[str, Any]:
    pack = store.get_pack(pack_id)
    if not pack:
        raise HTTPException(status_code=404, detail="Pack not found")
    result = pack_validator.validate_pack(pack_id, pack["file_path"], pack["checksum"])
    return result


@app.get("/deployment/packs/versions/{pack_id}")
def pack_versions(pack_id: str, _: None = Depends(require_admin)) -> dict[str, Any]:
    versions = incremental_distribution.get_version_history(pack_id)
    return {"pack_id": pack_id, "versions": versions}


@app.post("/deployment/packs/broadcast")
def create_broadcast(
    broadcast_id: str,
    pack_id: str,
    target_devices: list[str] | None = None,
    _: None = Depends(require_admin),
) -> dict[str, Any]:
    return broadcast_coordinator.create_broadcast(
        broadcast_id,
        pack_id,
        target_devices or [],
    )


# Deployment Validation Phase 6: Cache Hardening
@app.get("/deployment/cache/validation")
def cache_validation(_: None = Depends(require_admin)) -> dict[str, Any]:
    invalid = cache_validator.get_invalid_entries()
    return {"invalid_entries": invalid}


@app.post("/deployment/cache/cleanup")
def cleanup_cache(days_old: int = 30, _: None = Depends(require_admin)) -> dict[str, Any]:
    return cache_cleanup.cleanup_stale_entries(days_old)


@app.get("/deployment/cache/storage")
def storage_info(_: None = Depends(require_admin)) -> dict[str, Any]:
    storage_diagnostics.record_usage_snapshot()
    usage = storage_diagnostics.get_cache_usage()
    trend = storage_diagnostics.get_storage_trend()
    return {"usage": usage, "trend": trend}


# Deployment Validation Phase 7: Backend Recovery
@app.get("/deployment/backend/deferred-status")
def deferred_status(_: None = Depends(require_admin)) -> dict[str, Any]:
    return deferred_sync_manager.get_deferred_status()


@app.get("/deployment/backend/connectivity")
def backend_connectivity(_: None = Depends(require_admin)) -> dict[str, Any]:
    return backend_recovery.get_backend_status()


@app.get("/deployment/backend/resumable-syncs")
def resumable_syncs(_: None = Depends(require_admin)) -> dict[str, Any]:
    return {"resumable_syncs": partial_sync_recovery.get_resumable_syncs()}


# Deployment Validation Phase 8: Monitoring
@app.post("/deployment/metrics/classroom")
def record_classroom_metrics(
    active_devices: int,
    total_devices: int,
    pending_syncs: int,
    cached_packs: int,
    _: None = Depends(require_admin),
) -> dict[str, Any]:
    return metrics_collector.collect_classroom_metrics(
        active_devices,
        total_devices,
        pending_syncs,
        cached_packs,
    )


@app.get("/deployment/metrics/classroom-summary")
def classroom_summary(_: None = Depends(require_admin)) -> dict[str, Any]:
    return metrics_collector.get_classroom_summary()


@app.get("/deployment/metrics/sync")
def sync_metrics_endpoint(hours: int = 24, _: None = Depends(require_admin)) -> dict[str, Any]:
    return sync_metrics.get_sync_metrics(hours)


@app.get("/deployment/metrics/network")
def network_health(_: None = Depends(require_admin)) -> dict[str, Any]:
    return network_diagnostics.get_network_health()


# Deployment Validation Phase 9: Real-world Validation
@app.get("/deployment/validation/hotspot")
def validate_hotspot(_: None = Depends(require_admin)) -> dict[str, Any]:
    return classroom_validator.validate_hotspot_networking()


@app.get("/deployment/validation/deployment")
def validate_deployment_ready(_: None = Depends(require_admin)) -> dict[str, Any]:
    return classroom_validator.validate_deployment_readiness()


@app.get("/deployment/validation/scenarios")
def validation_scenarios(_: None = Depends(require_admin)) -> dict[str, Any]:
    return {"scenarios": recovery_scenarios.get_all_scenarios()}


@app.get("/", response_class=HTMLResponse)
@app.get("/dashboard", response_class=HTMLResponse)
def teacher_dashboard() -> str:
    return """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>PiHub Classroom Hub & Teacher Dashboard</title>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
  <style>
    :root {
      --bg: #0f172a;
      --card-bg: #1e293b;
      --accent: #38bdf8;
      --text: #f8fafc;
      --text-muted: #94a3b8;
      --success: #22c55e;
      --warning: #eab308;
      --border: #334155;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; font-family: 'Inter', sans-serif; }
    body { background: var(--bg); color: var(--text); padding: 24px; }
    header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 24px; padding-bottom: 16px; border-bottom: 1px solid var(--border); }
    h1 { font-size: 24px; font-weight: 700; color: var(--accent); }
    .badge { background: #0284c722; color: var(--accent); padding: 6px 12px; border-radius: 20px; font-size: 13px; border: 1px solid #0284c744; }
    .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 16px; margin-bottom: 24px; }
    .card { background: var(--card-bg); padding: 20px; border-radius: 12px; border: 1px solid var(--border); }
    .card-title { font-size: 13px; text-transform: uppercase; letter-spacing: 0.5px; color: var(--text-muted); margin-bottom: 8px; }
    .metric { font-size: 32px; font-weight: 700; color: var(--text); }
    .metric-sub { font-size: 13px; color: var(--success); margin-top: 4px; }
    table { width: 100%; border-collapse: collapse; margin-top: 12px; }
    th, td { text-align: left; padding: 12px; border-bottom: 1px solid var(--border); font-size: 14px; }
    th { color: var(--text-muted); font-weight: 500; }
    .status-dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; background: var(--success); margin-right: 6px; }
    .refresh-btn { background: var(--accent); color: #000; border: none; padding: 8px 16px; border-radius: 6px; font-weight: 600; cursor: pointer; }
    .refresh-btn:hover { opacity: 0.9; }
  </style>
</head>
<body>
  <header>
    <div>
      <h1>PiHub Classroom Dashboard</h1>
      <p style="color: var(--text-muted); font-size: 14px; margin-top: 4px;">Offline Educational Node & Decentralized Peer Sync</p>
    </div>
    <div style="display: flex; gap: 12px; align-items: center;">
      <span class="badge"><span class="status-dot"></span>Online Node</span>
      <button class="refresh-btn" onclick="fetchDashboardData()">Refresh Data</button>
    </div>
  </header>

  <div class="grid">
    <div class="card">
      <div class="card-title">Connected Devices</div>
      <div class="metric" id="devices-count">--</div>
      <div class="metric-sub">Registered Student Tablets</div>
    </div>
    <div class="card">
      <div class="card-title">Educational Content Packs</div>
      <div class="metric" id="packs-count">--</div>
      <div class="metric-sub">Installable Modules Active</div>
    </div>
    <div class="card">
      <div class="card-title">Qdrant Vector Chunks</div>
      <div class="metric" id="chunks-count">--</div>
      <div class="metric-sub">Indexed Offline Knowledge</div>
    </div>
    <div class="card">
      <div class="card-title">Active Sync Sessions</div>
      <div class="metric" id="sync-count">--</div>
      <div class="metric-sub">P2P Classroom Queue</div>
    </div>
  </div>

  <div class="card" style="margin-bottom: 24px;">
    <div class="card-title">Registered Classroom Devices</div>
    <table>
      <thead>
        <tr>
          <th>Device Name</th>
          <th>Student Name</th>
          <th>Grade</th>
          <th>Device ID</th>
          <th>Last Active</th>
        </tr>
      </thead>
      <tbody id="devices-table">
        <tr><td colspan="5" style="color: var(--text-muted);">Loading devices...</td></tr>
      </tbody>
    </table>
  </div>

  <script>
    async function fetchDashboardData() {
      try {
        const healthRes = await fetch('/health');
        if (healthRes.ok) {
          const health = await healthRes.json();
          document.getElementById('packs-count').innerText = health.content_packs || 0;
          document.getElementById('chunks-count').innerText = health.rag_chunks || 0;
        }

        const devicesRes = await fetch('/devices');
        if (devicesRes.ok) {
          const devicesData = await devicesRes.json();
          const devices = Array.isArray(devicesData) ? devicesData : (devicesData.devices || []);
          document.getElementById('devices-count').innerText = devices.length;

          const tbody = document.getElementById('devices-table');
          if (devices.length === 0) {
            tbody.innerHTML = '<tr><td colspan="5" style="color: var(--text-muted);">No student devices registered yet.</td></tr>';
          } else {
            tbody.innerHTML = devices.map(d => `
              <tr>
                <td><strong>${d.device_name || 'Student Device'}</strong></td>
                <td>${d.student_name || 'Anonymous Student'}</td>
                <td>Grade ${d.grade || 6}</td>
                <td style="font-family: monospace; font-size: 12px; color: var(--text-muted);">${d.device_id || '--'}</td>
                <td><span class="status-dot"></span>Just now</td>
              </tr>
            `).join('');
          }
        }

        const syncRes = await fetch('/deployment/metrics/classroom-summary');
        if (syncRes.ok) {
          const syncData = await syncRes.json();
          document.getElementById('sync-count').innerText = syncData.pending_syncs || 0;
        } else {
          document.getElementById('sync-count').innerText = '0';
        }
      } catch (e) {
        console.error('Dashboard fetch error:', e);
      }
    }

    fetchDashboardData();
    setInterval(fetchDashboardData, 10000);
  </script>
</body>
</html>"""

