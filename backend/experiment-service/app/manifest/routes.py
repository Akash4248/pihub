from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.encoders import jsonable_encoder

from shared.security import require_admin

from app.models.execution_package import ExecutionPackageRequest
from app.models.execution_resolution import ExecutionResolutionRequest
from app.services.execution_resolver import ExecutionDefinitionError, ExecutionResolverService, ManifestNotFoundError
from app.services.execution_package_service import ExecutionPackageService
from app.services.manifest_resolver import (
    ManifestHashMismatchError,
    ManifestNotFoundError as ResolvedManifestNotFoundError,
    ManifestUnavailableError,
)
from app.services.manifest_migration_service import ManifestMigrationError, ManifestMigrationService
from app.services.manifest_version_service import ManifestVersionService

from .manifest_service import ExperimentManifestService


router = APIRouter(tags=["Experiment Manifest"])
manifest_service = ExperimentManifestService()
execution_resolver = ExecutionResolverService(manifest_service)
execution_package_service = ExecutionPackageService(manifest_service, execution_resolver)
manifest_version_service = ManifestVersionService()
manifest_migration_service = ManifestMigrationService()


def _dump(model: Any) -> dict[str, Any]:
    if hasattr(model, "model_dump"):
        return model.model_dump(mode="json")
    if hasattr(model, "dict"):
        return jsonable_encoder(model.dict())
    return jsonable_encoder(dict(model))


def _parse_model(model_type: Any, payload: dict[str, Any]) -> Any:
    if hasattr(model_type, "model_validate"):
        return model_type.model_validate(payload)
    return model_type.parse_obj(payload)


@router.get("/manifest/templates")
async def manifest_templates() -> dict[str, Any]:
    templates = manifest_service.list_templates()
    return {
        "templates": [_dump(template) for template in templates],
        "total": len(templates),
    }


@router.get("/manifest/templates/{template_id}")
async def manifest_template(template_id: str) -> dict[str, Any]:
    template = manifest_service.get_template(template_id)
    if template is None:
        raise HTTPException(status_code=404, detail="Manifest template not found")
    return _dump(template)


@router.get("/manifest/templates/{template_id}/execution")
async def manifest_template_execution(template_id: str) -> dict[str, Any]:
    template = manifest_service.get_template(template_id)
    if template is None:
        raise HTTPException(status_code=404, detail="Manifest template not found")
    execution = template.manifest.get("execution")
    if not isinstance(execution, dict):
        raise HTTPException(status_code=404, detail="Execution definition not found")
    return execution


@router.post("/execution-package")
async def execution_package(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        request = _parse_model(ExecutionPackageRequest, payload)
        return _dump(execution_package_service.build_package(request))
    except (ManifestNotFoundError, ResolvedManifestNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ManifestUnavailableError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ManifestHashMismatchError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ExecutionDefinitionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/manifest/validate")
async def validate_manifest(payload: dict[str, Any]) -> dict[str, Any]:
    manifest = payload.get("manifest") if "manifest" in payload else payload
    return _dump(manifest_service.validate(manifest))


@router.post("/manifest/scene/validate")
async def validate_scene(payload: dict[str, Any]) -> dict[str, Any]:
    scene = payload.get("scene") if "scene" in payload else payload
    return _dump(manifest_service.validate_scene(scene))


@router.post("/manifest/execution/validate")
async def validate_execution(payload: dict[str, Any]) -> dict[str, Any]:
    execution = payload.get("execution") if "execution" in payload else payload
    return _dump(manifest_service.validate_execution(execution))


@router.post("/manifest/capability-check")
async def manifest_capability_check(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        request = _parse_model(ExecutionResolutionRequest, payload)
        return _dump(execution_resolver.capability_check(request))
    except ManifestNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ExecutionDefinitionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/manifest/resolve")
async def manifest_resolve(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        request = _parse_model(ExecutionResolutionRequest, payload)
        return _dump(execution_resolver.resolve(request))
    except ManifestNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ExecutionDefinitionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/manifest/versions")
async def manifest_versions() -> dict[str, Any]:
    return manifest_version_service.versions()


@router.post("/manifest/compatibility")
async def manifest_compatibility(payload: dict[str, Any]) -> dict[str, Any]:
    manifest = payload.get("manifest") if "manifest" in payload else payload
    return manifest_version_service.check_compatibility(manifest)


@router.post("/manifest/migrate", dependencies=[Depends(require_admin)])
async def migrate_manifest(payload: dict[str, Any]) -> dict[str, Any]:
    manifest = payload.get("manifest") if "manifest" in payload else payload
    target_version = str(payload.get("target_version") or manifest_version_service.versions()["current_version"])
    try:
        return manifest_migration_service.migrate(manifest, target_version)
    except ManifestMigrationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
