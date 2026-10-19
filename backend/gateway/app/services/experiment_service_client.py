from __future__ import annotations

import logging
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import HTTPException

from shared.security import get_internal_service_key


logger = logging.getLogger("gateway.experiment_service")


class ExperimentGatewayMetrics:
    def __init__(self) -> None:
        self.experiment_requests = 0
        self.experiment_errors = 0
        self.successful_runs = 0
        self.failed_runs = 0
        self.analytics_requests = 0
        self.latencies_ms: deque[float] = deque(maxlen=200)
        self.recent: deque[dict[str, Any]] = deque(maxlen=50)

    def record(self, path: str, method: str, status_code: int, latency_ms: float, analytics: bool = False) -> None:
        self.experiment_requests += 1
        if status_code >= 400:
            self.experiment_errors += 1
        if path == "/experiment-runs" and method == "POST" and status_code < 400:
            self.successful_runs += 1
        if path.endswith("/complete") and status_code >= 400:
            self.failed_runs += 1
        if analytics:
            self.analytics_requests += 1
        self.latencies_ms.append(latency_ms)
        self.recent.appendleft({
            "recorded_at": datetime.now(tz=timezone.utc).isoformat(),
            "method": method,
            "path": path,
            "status_code": status_code,
            "latency_ms": round(latency_ms, 2),
        })

    def snapshot(self) -> dict[str, Any]:
        count = len(self.latencies_ms)
        average_latency = round(sum(self.latencies_ms) / count, 2) if count else 0.0
        return {
            "experiment_requests": self.experiment_requests,
            "experiment_errors": self.experiment_errors,
            "experiment_latency_ms": average_latency,
            "successful_runs": self.successful_runs,
            "failed_runs": self.failed_runs,
            "analytics_requests": self.analytics_requests,
            "recent": list(self.recent),
        }


class ExperimentServiceClient:
    def __init__(
        self,
        http: httpx.AsyncClient,
        base_url: str,
        metrics: ExperimentGatewayMetrics,
        timeout_seconds: float = 15.0,
        retries: int = 1,
    ) -> None:
        self.http = http
        self.base_url = base_url.rstrip("/")
        self.metrics = metrics
        self.timeout_seconds = timeout_seconds
        self.retries = retries

    @staticmethod
    def _auth_headers() -> dict[str, str]:
        # experiment-service enforces fail-closed auth; gateway hops must
        # authenticate with the shared internal service key.
        return {"X-Internal-Service-Key": get_internal_service_key()}

    async def health(self) -> dict[str, Any]:
        try:
            response = await self.http.get(
                f"{self.base_url}/experiments",
                timeout=self.timeout_seconds,
                headers=self._auth_headers(),
            )
            healthy = response.is_success
            logger.info("[EXPERIMENT_GATEWAY] SERVICE_HEALTH healthy=%s status=%s", healthy, response.status_code)
            return {"healthy": healthy, "status_code": response.status_code}
        except Exception as exc:
            logger.info("[EXPERIMENT_GATEWAY] SERVICE_HEALTH healthy=false (optional service offline: %s)", exc)
            return {"healthy": False, "error": str(exc), "status": "optional_service_offline"}

    async def get_experiments(self, params: dict[str, Any] | None = None) -> Any:
        return await self._request("GET", "/experiments", params=params)

    async def get_catalog(self) -> Any:
        return await self._request("GET", "/experiments/catalog")

    async def get_experiment(self, experiment_id: str) -> Any:
        return await self._request("GET", f"/experiments/{experiment_id}")

    async def get_certification(self, experiment_id: str) -> Any:
        return await self._request("GET", f"/experiments/{experiment_id}/certification")

    async def get_chapter_experiments(self, chapter_id: str) -> Any:
        return await self._request("GET", f"/chapters/{chapter_id}/experiments")

    async def search_experiments(self, params: dict[str, Any] | None = None) -> Any:
        return await self._request("GET", "/experiments/search", params=params)

    async def get_templates(self) -> Any:
        return await self._request("GET", "/experiment-templates")

    async def create_run(self, payload: dict[str, Any]) -> Any:
        return await self._request("POST", "/experiment-runs", json=payload)

    async def get_run(self, run_id: str) -> Any:
        return await self._request("GET", f"/experiment-runs/{run_id}")

    async def append_event(self, run_id: str, payload: dict[str, Any]) -> Any:
        return await self._request("POST", f"/experiment-runs/{run_id}/events", json=payload)

    async def complete_run(self, run_id: str, payload: dict[str, Any]) -> Any:
        return await self._request("POST", f"/experiment-runs/{run_id}/complete", json=payload)

    async def get_student_runs(self, student_id: str) -> Any:
        return await self._request("GET", f"/experiment-runs/student/{student_id}")

    async def get_student_analytics(self, student_id: str) -> Any:
        return await self._request("GET", f"/analytics/student/{student_id}", analytics=True)

    async def get_experiment_analytics(self, experiment_id: str) -> Any:
        return await self._request("GET", f"/analytics/experiment/{experiment_id}", analytics=True)

    async def get_system_analytics(self) -> Any:
        return await self._request("GET", "/analytics/system", analytics=True)

    async def get_top_experiments(self, params: dict[str, Any] | None = None) -> Any:
        return await self._request("GET", "/analytics/top-experiments", params=params, analytics=True)

    async def get_metrics(self) -> Any:
        return await self._request("GET", "/experiment-metrics", analytics=True)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        analytics: bool = False,
    ) -> Any:
        url = f"{self.base_url}{path}"
        started = time.perf_counter()
        logger.info("[EXPERIMENT_GATEWAY] REQUEST_START method=%s path=%s", method, path)
        last_exc: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                response = await self.http.request(
                    method,
                    url,
                    params=params,
                    json=json,
                    timeout=self.timeout_seconds,
                    headers=self._auth_headers(),
                )
                latency_ms = (time.perf_counter() - started) * 1000
                self.metrics.record(path, method, response.status_code, latency_ms, analytics=analytics)
                logger.info("[EXPERIMENT_GATEWAY] LATENCY_MS path=%s latency_ms=%.2f", path, latency_ms)
                logger.info("[EXPERIMENT_GATEWAY] REQUEST_END method=%s path=%s status=%s", method, path, response.status_code)
                if response.is_error:
                    raise HTTPException(status_code=response.status_code, detail=self._detail(response))
                return response.json()
            except HTTPException:
                raise
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_exc = exc
                if attempt >= self.retries:
                    break

        latency_ms = (time.perf_counter() - started) * 1000
        self.metrics.record(path, method, 502, latency_ms, analytics=analytics)
        logger.error("[EXPERIMENT_GATEWAY] ERROR method=%s path=%s error=%s", method, path, last_exc)
        raise HTTPException(status_code=502, detail="Experiment service unavailable")

    @staticmethod
    def _detail(response: httpx.Response) -> Any:
        try:
            body = response.json()
        except ValueError:
            return response.text
        if isinstance(body, dict) and "detail" in body:
            return body["detail"]
        return body
