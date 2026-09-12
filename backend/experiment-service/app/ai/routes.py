from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.encoders import jsonable_encoder

from app.ai.models import ExperimentExplanationRequest, ExperimentGenerationRequest, ExperimentRefineRequest
from app.ai.services.ai_experiment_generator_service import AIExperimentGeneratorService


router = APIRouter(tags=["AI Experiment Authoring"])
ai_experiment_service = AIExperimentGeneratorService()


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


@router.post("/ai/generate-experiment")
@router.post("/ai/experiment/generate")
async def generate_experiment(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        request = _parse_model(ExperimentGenerationRequest, payload)
        return _dump(ai_experiment_service.generate(request))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/ai/refine-experiment")
async def refine_experiment(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        request = _parse_model(ExperimentRefineRequest, payload)
        return _dump(ai_experiment_service.refine(request))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/ai/explain-experiment")
async def explain_experiment(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        request = _parse_model(ExperimentExplanationRequest, payload)
        return _dump(ai_experiment_service.explain(request))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
