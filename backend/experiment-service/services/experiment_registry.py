from __future__ import annotations

import logging

from domain.enums import ExperimentDifficulty, ExperimentExecutionMode
from repositories.experiment_manifest_repository import JsonExperimentManifestRepository
from schemas.experiment_registry_models import (
    ExperimentDefinition,
    ExperimentRegistryMetadata,
    ExperimentSearchFilters,
    ExperimentSummary,
)


logger = logging.getLogger("experiment-service.registry")


def _model_dump(model: object) -> dict[str, object]:
    if hasattr(model, "model_dump"):
        return model.model_dump(mode="json")  # type: ignore[attr-defined]
    if hasattr(model, "dict"):
        return model.dict()  # type: ignore[attr-defined]
    return {}


class ExperimentRegistry:
    def __init__(self, repository: JsonExperimentManifestRepository | None = None) -> None:
        self.repository = repository or JsonExperimentManifestRepository()
        logger.info("[EXPERIMENT] REGISTRY_INITIALIZED")

    def register_experiment(self, experiment: ExperimentDefinition) -> ExperimentDefinition:
        saved = self.repository.save_experiment(experiment)
        logger.info("[EXPERIMENT] EXPERIMENT_REGISTERED=%s", saved.manifest.id)
        return saved

    def update_experiment(self, experiment: ExperimentDefinition) -> ExperimentDefinition:
        saved = self.repository.save_experiment(experiment)
        logger.info("[EXPERIMENT] EXPERIMENT_REGISTERED=%s", saved.manifest.id)
        return saved

    def delete_experiment(self, experiment_id: str) -> bool:
        return self.repository.delete_experiment(experiment_id)

    def get_experiment(self, experiment_id: str) -> ExperimentDefinition | None:
        experiment = self.repository.get_experiment(experiment_id)
        logger.info("[EXPERIMENT] EXPERIMENT_LOADED=%s", experiment_id)
        return experiment

    def list_experiments(self, filters: ExperimentSearchFilters | None = None) -> list[ExperimentDefinition]:
        experiments = self.repository.list_experiments()
        return self._apply_filters(experiments, filters or ExperimentSearchFilters())

    def search_experiments(self, filters: ExperimentSearchFilters) -> list[ExperimentDefinition]:
        logger.info("[EXPERIMENT] SEARCH_QUERY=%s", _model_dump(filters))
        results = self._apply_filters(self.repository.list_experiments(), filters)
        logger.info("[EXPERIMENT] SEARCH_RESULTS=%s", len(results))
        return results

    def subjects(self) -> list[str]:
        return sorted({experiment.manifest.subject for experiment in self.repository.list_experiments()})

    def topics(self) -> list[str]:
        return sorted({experiment.manifest.topic for experiment in self.repository.list_experiments() if experiment.manifest.topic})

    def metadata(self) -> ExperimentRegistryMetadata:
        experiments = self.repository.list_experiments()
        updated_at = max((experiment.manifest.updated_at for experiment in experiments), default=None)
        if updated_at is None:
            from datetime import datetime, timezone

            updated_at = datetime.now(tz=timezone.utc)
        return ExperimentRegistryMetadata(
            total=len(experiments),
            subjects=self.subjects(),
            topics=self.topics(),
            updated_at=updated_at,
        )

    @staticmethod
    def summarize(experiment: ExperimentDefinition) -> ExperimentSummary:
        manifest = experiment.manifest
        return ExperimentSummary(
            id=manifest.id,
            title=manifest.title,
            subject=manifest.subject,
            category=experiment.category,
            chapter=manifest.chapter,
            topic=manifest.topic,
            difficulty=manifest.difficulty,
            required_sensors=manifest.required_sensors,
            supported_modes=manifest.supported_modes,
            estimated_duration=manifest.estimated_duration_minutes,
            description=manifest.description,
        )

    def _apply_filters(
        self,
        experiments: list[ExperimentDefinition],
        filters: ExperimentSearchFilters,
    ) -> list[ExperimentDefinition]:
        results = experiments
        if filters.q:
            query = filters.q.lower()
            results = [
                experiment
                for experiment in results
                if query in " ".join(
                    [
                        experiment.manifest.title,
                        experiment.manifest.description,
                        experiment.manifest.subject,
                        experiment.manifest.chapter or "",
                        experiment.manifest.topic or "",
                        " ".join(experiment.manifest.tags),
                    ]
                ).lower()
            ]
        if filters.grade is not None:
            results = [experiment for experiment in results if experiment.manifest.grade == filters.grade]
        if filters.subject:
            results = [experiment for experiment in results if experiment.manifest.subject.lower() == filters.subject.lower()]
        if filters.chapter:
            results = [experiment for experiment in results if (experiment.manifest.chapter or "").lower() == filters.chapter.lower()]
        if filters.topic:
            topic = filters.topic.lower()
            results = [experiment for experiment in results if topic in (experiment.manifest.topic or "").lower()]
        if filters.difficulty:
            difficulty = ExperimentDifficulty(filters.difficulty)
            results = [experiment for experiment in results if experiment.manifest.difficulty == difficulty]
        if filters.required_sensors:
            required = {sensor.lower() for sensor in filters.required_sensors}
            results = [
                experiment
                for experiment in results
                if required.issubset({sensor.lower() for sensor in experiment.manifest.required_sensors})
            ]
        if filters.execution_modes:
            modes = {ExperimentExecutionMode(mode) for mode in filters.execution_modes}
            results = [
                experiment
                for experiment in results
                if modes.intersection(set(experiment.manifest.supported_modes))
            ]
        if filters.tags:
            tags = {tag.lower() for tag in filters.tags}
            results = [
                experiment
                for experiment in results
                if tags.intersection({tag.lower() for tag in experiment.manifest.tags})
            ]
        return results
