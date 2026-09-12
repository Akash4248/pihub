from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.encoders import jsonable_encoder

from app.sharing.models import ShareExportRequest, ShareImportRequest, ShareSignRequest, ShareTrustRequest, ShareVerifyRequest
from app.sharing.services.experiment_sharing_service import ExperimentSharingService, SharingNotFoundError, SharingValidationError
from shared.security import verify_internal_api_key


router = APIRouter(tags=["Experiment Sharing"])
sharing_service = ExperimentSharingService()


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


@router.post("/sharing/export")
async def export_share_package(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        request = _parse_model(ShareExportRequest, payload)
        return _dump(sharing_service.export_package(request))
    except SharingNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except SharingValidationError as exc:
        raise HTTPException(status_code=400, detail={"errors": exc.errors}) from exc


@router.post("/sharing/import")
async def import_share_package(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        request = _parse_model(ShareImportRequest, payload)
        return _dump(sharing_service.import_package(request))
    except SharingValidationError as exc:
        raise HTTPException(status_code=400, detail={"errors": exc.errors}) from exc


@router.post("/sharing/verify")
async def verify_share_package(payload: dict[str, Any]) -> dict[str, Any]:
    request = _parse_model(ShareVerifyRequest, payload)
    return _dump(sharing_service.verify_package(request.package))


@router.post("/sharing/sign", dependencies=[Depends(verify_internal_api_key)])
async def sign_share_package(payload: dict[str, Any]) -> dict[str, Any]:
    request = _parse_model(ShareSignRequest, payload)
    return _dump(sharing_service.sign_package(request.package))


@router.post("/sharing/trust", dependencies=[Depends(verify_internal_api_key)])
async def trust_share_source(payload: dict[str, Any]) -> dict[str, Any]:
    request = _parse_model(ShareTrustRequest, payload)
    return sharing_service.trust(request)


@router.get("/sharing/analytics")
async def sharing_analytics() -> dict[str, Any]:
    return _dump(sharing_service.analytics())
