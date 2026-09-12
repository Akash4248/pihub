from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query
from fastapi.encoders import jsonable_encoder

from domain.enums import ExperimentDifficulty, ExperimentExecutionMode
from schemas.contracts import AppendExperimentEventRequest, CompleteExperimentRunRequest, CreateExperimentRunRequest
from schemas.experiment_registry_models import ExperimentSearchFilters
from analytics.experiment_analytics_service import ExperimentAnalyticsService
from app.core.pagination import page_query, page_size_query, paginate
from app.experiment_content.service import ExperimentContentService
from services.experiment_registry import ExperimentRegistry
from services.experiment_run_service import ExperimentRunService


logger = logging.getLogger("experiment-service.api")
router = APIRouter(tags=["Experiment Engine"])
registry = ExperimentRegistry()
run_service = ExperimentRunService()
analytics_service = ExperimentAnalyticsService()
content_service = ExperimentContentService()


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
@router.get("/experiments")
async def list_experiments(
    grade: int | None = Query(default=None),
    subject: str | None = Query(default=None),
    chapter: str | None = Query(default=None),
    topic: str | None = Query(default=None),
    difficulty: ExperimentDifficulty | None = Query(default=None),
    required_sensors: list[str] | None = Query(default=None),
    execution_modes: list[ExperimentExecutionMode] | None = Query(default=None),
    tags: list[str] | None = Query(default=None),
    page: int = page_query(),
    page_size: int = page_size_query(),
) -> dict[str, Any]:
    filters = ExperimentSearchFilters(
        grade=grade,
        subject=subject,
        chapter=chapter,
        topic=topic,
        difficulty=difficulty,
        required_sensors=required_sensors or [],
        execution_modes=execution_modes or [],
        tags=tags or [],
    )
    experiments = registry.list_experiments(filters)
    paged = paginate(experiments, page, page_size)
    return {
        "experiments": [_dump(registry.summarize(experiment)) for experiment in paged["items"]],
        "page": page,
        "page_size": page_size,
        "total": len(experiments),
        "metadata": _dump(registry.metadata()),
    }


@router.get("/experiments/search")
async def search_experiments(
    q: str | None = Query(default=None),
    grade: int | None = Query(default=None),
    subject: str | None = Query(default=None),
    chapter: str | None = Query(default=None),
    topic: str | None = Query(default=None),
    difficulty: ExperimentDifficulty | None = Query(default=None),
    required_sensors: list[str] = Query(default=[]),
    execution_modes: list[ExperimentExecutionMode] = Query(default=[]),
    tags: list[str] = Query(default=[]),
    page: int = page_query(),
    page_size: int = page_size_query(),
) -> dict[str, Any]:
    filters = ExperimentSearchFilters(
        q=q,
        grade=grade,
        subject=subject,
        chapter=chapter,
        topic=topic,
        difficulty=difficulty,
        required_sensors=required_sensors,
        execution_modes=execution_modes,
        tags=tags,
    )
    experiments = registry.search_experiments(filters)
    paged = paginate(experiments, page, page_size)
    return {
        "query": _dump(filters),
        "experiments": [_dump(registry.summarize(experiment)) for experiment in paged["items"]],
        "page": page,
        "page_size": page_size,
        "total": len(experiments),
    }


@router.get("/experiments/subjects")
async def experiment_subjects() -> dict[str, Any]:
    subjects = registry.subjects()
    return {"subjects": subjects, "total": len(subjects)}


@router.get("/experiments/topics")
async def experiment_topics() -> dict[str, Any]:
    topics = registry.topics()
    return {"topics": topics, "total": len(topics)}


@router.get("/experiments/{experiment_id}")
async def get_experiment(experiment_id: str) -> dict[str, Any]:
    experiment = registry.get_experiment(experiment_id)
    if experiment is None:
        raise HTTPException(status_code=404, detail="Experiment not found")
    content = content_service.experiment(experiment_id)
    return {
        **_dump(registry.summarize(experiment)),
        "category": experiment.category,
        "version": experiment.version,
        "manifest": _dump(experiment.manifest),
        "steps": [_dump(step) for step in experiment.steps],
        "variables": [_dump(variable) for variable in experiment.variables],
        "visualizations": [_dump(visualization) for visualization in experiment.visualizations],
        "metadata": experiment.metadata,
        "learning_content": content["learning_content"],
        "flashcards": content["flashcards"],
        "quiz": content["quiz"],
        "glossary": content["glossary"],
        "summary": content["summary"],
        "certification": content["certification"],
        "download_url": content["download_url"],
    }


@router.get("/experiment-templates")
async def list_experiment_templates(page: int = page_query(), page_size: int = page_size_query()) -> dict[str, Any]:
    experiments = registry.list_experiments()
    paged = paginate(experiments, page, page_size)
    templates = [
        {
            "id": f"{experiment.manifest.id}-template",
            "title": experiment.manifest.title,
            "category": experiment.category,
            "manifest": _dump(experiment.manifest),
        }
        for experiment in paged["items"]
    ]
    return {"templates": templates, "page": page, "page_size": page_size, "total": len(experiments)}


@router.post("/experiment-runs")
async def create_experiment_run(payload: dict[str, Any]) -> dict[str, Any]:
    request = _parse_model(CreateExperimentRunRequest, payload)
    run = await run_service.create_run(request)
    return {"run_id": run.run_id, "status": run.status.value}


@router.get("/experiment-runs/student/{student_id}")
async def list_student_experiment_runs(
    student_id: str,
    x_user_id: str | None = Header(None, alias="x-user-id"),
    x_user_role: str | None = Header(None, alias="x-user-role"),
) -> list[dict[str, Any]]:
    # IDOR Protection: Enforce caller identity matches student_id or role is admin/teacher
    if not x_user_id:
        raise HTTPException(status_code=401, detail="Authentication required: x-user-id header missing.")
    if x_user_id != student_id and x_user_role not in {"admin", "teacher"}:
        logger.warning("[SECURITY] IDOR attempt: user %s requested runs for student %s", x_user_id, student_id)
        raise HTTPException(status_code=403, detail="Forbidden: Access to specified student runs is unauthorized.")
    runs = await run_service.list_student_runs(student_id)
    return [_dump(run) for run in runs]


@router.get("/experiment-runs/{run_id}")
async def get_experiment_run(run_id: str) -> dict[str, Any]:
    run = await run_service.load_run(run_id)
    return _dump(run)


@router.post("/experiment-runs/{run_id}/events")
async def append_experiment_run_event(run_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    request = _parse_model(AppendExperimentEventRequest, payload)
    await run_service.append_event(run_id, request)
    return {"success": True}


@router.get("/experiment-runs/{run_id}/events")
async def get_experiment_run_events(run_id: str) -> list[dict[str, Any]]:
    events = await run_service.get_events(run_id)
    return [_dump(event) for event in events]


@router.post("/experiment-runs/{run_id}/complete")
async def complete_experiment_run(run_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    request = _parse_model(CompleteExperimentRunRequest, payload)
    run = await run_service.complete_run(run_id, request)
    return {"status": run.status.value}


@router.get("/analytics/student/{student_id}")
async def student_analytics(student_id: str) -> dict[str, Any]:
    return _dump(analytics_service.student_analytics(student_id))


@router.get("/analytics/experiment/{experiment_id}")
async def experiment_analytics(experiment_id: str) -> dict[str, Any]:
    return _dump(analytics_service.experiment_analytics(experiment_id))


@router.get("/analytics/system")
async def system_analytics() -> dict[str, Any]:
    return _dump(analytics_service.system_analytics())


@router.get("/analytics/top-experiments")
async def top_experiments(limit: int = Query(default=10, ge=1, le=100)) -> list[dict[str, Any]]:
    return [_dump(item) for item in analytics_service.top_experiments(limit)]


@router.get("/experiment-metrics")
async def experiment_metrics() -> dict[str, int]:
    return analytics_service.metrics()
