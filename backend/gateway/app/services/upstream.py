"""Proxy layer for gateway -> downstream service calls.

All calls authenticate with the shared internal service key so downstream
services can stay locked down regardless of the caller.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import httpx
from fastapi import HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

from shared.config import get_settings
from shared.security import get_internal_service_key, read_file_chunks_safely, sanitize_safe_filename
from shared.discovery import get_service_registry
from shared.resilience import get_circuit_breaker_registry
from shared.tracing import get_tracer
from shared.logging import current_request_id
from shared.auth import issue_service_token

from app.context import http_client


logger = logging.getLogger(__name__)

settings = get_settings()

# Per-route downstream timeouts (seconds). The shared client ceiling stays at
# settings.gateway_http_timeout_seconds; these bound each proxied call so one
# hung service cannot hold gateway workers for the full ceiling.
DEFAULT_PROXY_TIMEOUT = 15.0
INGEST_PROXY_TIMEOUT = 45.0
AI_PROXY_TIMEOUT = 120.0


def resolve_service_name(base_url: str) -> str:
    url_lower = base_url.lower()
    if "inference" in url_lower or ":8010" in url_lower:
        return "inference-service"
    if "content-pipeline" in url_lower or ":8001" in url_lower:
        return "content-pipeline"
    if "pack" in url_lower or ":8030" in url_lower:
        return "pack-service"
    if "pihub" in url_lower or ":8020" in url_lower:
        return "pihub"
    if "voice" in url_lower or ":8050" in url_lower:
        return "voice-service"
    return "downstream-service"


def prepare_proxy_headers(headers: dict[str, str] | None, target_service: str) -> dict[str, str]:
    proxy_headers = dict(headers or {})
    proxy_headers.setdefault("X-Internal-Service-Key", get_internal_service_key())

    # Distributed context propagation
    req_id = current_request_id.get()
    if req_id:
        proxy_headers.setdefault("X-Request-ID", req_id)

    get_tracer(service_name="gateway").inject_headers(proxy_headers)

    # Inter-Service Zero-Trust token
    try:
        token = issue_service_token("gateway", audience=target_service)
        proxy_headers.setdefault("X-Service-Token", token)
    except Exception:
        pass

    return proxy_headers


def error_detail(text: str) -> Any:
    try:
        body = json.loads(text)
    except json.JSONDecodeError:
        return text
    if isinstance(body, dict) and "detail" in body:
        return body["detail"]
    return body


async def proxy_request(
    base_url: str,
    path: str,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    files: dict[str, Any] | None = None,
    stream: bool = False,
    timeout: float = DEFAULT_PROXY_TIMEOUT,
) -> Any:
    service_name = resolve_service_name(base_url)
    resolved_url = get_service_registry().resolve(service_name, default_url=base_url)
    proxy_headers = prepare_proxy_headers(headers, service_name)

    if stream:
        return await proxy_stream(resolved_url, method, path, payload=payload, headers=proxy_headers, timeout=timeout)

    cb = get_circuit_breaker_registry().get_breaker(service_name)

    async def _execute_call():
        url = f"{resolved_url}{path}"
        try:
            return await http_client().request(
                method, url, json=payload, params=params, headers=proxy_headers, files=files,
                timeout=timeout,
            )
        except httpx.RequestError as exc:
            get_service_registry().mark_unhealthy(service_name, resolved_url)
            raise HTTPException(status_code=503, detail=f"Service unavailable: {str(exc)}") from exc

    response = await cb.call(_execute_call)

    if response.is_error:
        raise HTTPException(status_code=response.status_code, detail=response.text[:2000])

    content_type = response.headers.get("content-type", "")
    if "application/json" in content_type or not content_type:
        try:
            return response.json()
        except json.JSONDecodeError:
            return {"text": response.text[:2000]}

    try:
        return response.json()
    except json.JSONDecodeError:
        return {"text": response.text[:2000]}


async def get_json(base_url: str, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    return await proxy_request(base_url, path, method="GET", params=params)


async def post_json(base_url: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
    return await proxy_request(base_url, path, method="POST", payload=payload)


async def post_multipart(
    base_url: str,
    path: str,
    file: UploadFile,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    safe_name = sanitize_safe_filename(file.filename or "file.bin")
    chunks = []
    async for chunk in read_file_chunks_safely(file, max_bytes=50 * 1024 * 1024):
        chunks.append(chunk)
    content = b"".join(chunks)
    files = {"file": (safe_name, content, file.content_type or "application/octet-stream")}
    return await proxy_request(base_url, path, method="POST", params=params, files=files)


async def send_with_retry(
    base_url: str,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    stream: bool = False,
    retries: int = 3,
    timeout: float = DEFAULT_PROXY_TIMEOUT,
) -> httpx.Response:
    service_name = resolve_service_name(base_url)
    resolved_base = get_service_registry().resolve(service_name, default_url=base_url)
    proxy_headers = prepare_proxy_headers(headers, service_name)
    cb = get_circuit_breaker_registry().get_breaker(service_name)

    async def _execute_attempt():
        url = f"{resolved_base}{path}"
        retryable = settings.voice_service_enabled and base_url == settings.voice_service_url
        delay = 0.25
        last_error: Exception | None = None

        for attempt in range(1, retries + 1):
            try:
                if stream:
                    request = http_client().build_request(method, url, json=payload, headers=proxy_headers)
                    return await http_client().send(request, stream=True, timeout=timeout)
                return await http_client().request(method, url, json=payload, headers=proxy_headers, timeout=timeout)
            except httpx.RequestError as exc:
                last_error = exc
                if not retryable or attempt >= retries:
                    break
                await asyncio.sleep(delay)
                delay *= 2

        get_service_registry().mark_unhealthy(service_name, resolved_base)
        raise HTTPException(status_code=503, detail=f"Service unavailable: {last_error}")

    return await cb.call(_execute_attempt)


async def proxy_stream(
    base_url: str,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = AI_PROXY_TIMEOUT,
) -> StreamingResponse:
    response = await send_with_retry(
        base_url,
        method,
        path,
        payload=payload,
        headers=headers,
        stream=True,
        timeout=timeout,
    )

    if response.is_error:
        body = await response.aread()
        body_text = body.decode("utf-8", errors="replace")
        await response.aclose()
        raise HTTPException(
            status_code=response.status_code,
            detail=error_detail(body_text),
        )

    content_type = response.headers.get("content-type", "application/octet-stream")
    passthrough_headers = {
        key: value
        for key in ("content-length", "content-disposition", "etag", "last-modified", "accept-ranges", "content-range", "cache-control")
        if (value := response.headers.get(key)) is not None
    }
    return StreamingResponse(
        response.aiter_raw(),
        status_code=response.status_code,
        media_type=content_type,
        headers=passthrough_headers,
        background=BackgroundTask(response.aclose),
    )


async def proxy_to(
    base_url: str,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    timeout: float = DEFAULT_PROXY_TIMEOUT,
) -> Any:
    if isinstance(payload, dict) and payload.get("stream") is True:
        return await proxy_stream(base_url, method, path, payload, timeout=timeout)

    response = await send_with_retry(base_url, method, path, payload=payload, timeout=timeout)

    if response.is_error:
        raise HTTPException(
            status_code=response.status_code,
            detail=error_detail(response.text)
        )

    content_type = response.headers.get("content-type", "")

    if "application/json" in content_type:
        return response.json()

    logger.error(
        "Unexpected content type: %s",
        content_type
    )

    logger.error(
        "Response body: %s",
        response.text[:2000]
    )

    raise HTTPException(
        status_code=502,
        detail=f"Unexpected response type from {base_url}{path}: {content_type}"
    )


async def safe_http_get_json(url: str, timeout: float = 3.0) -> tuple[bool, Any]:
    """Best-effort GET for health fan-outs; never raises, never authenticates blindly."""
    # Downstream services enforce fail-closed auth; the health fan-out must
    # authenticate with the internal service key (Qdrant simply ignores it).
    headers = {"X-Internal-Service-Key": get_internal_service_key()}
    try:
        resp = await http_client().get(url, timeout=timeout, headers=headers)
        if resp.is_error:
            try:
                return False, resp.json()
            except Exception:
                return False, {"status": "error", "status_code": resp.status_code, "text": resp.text[:500]}
        try:
            return True, resp.json()
        except Exception:
            return True, {"status": "ok", "text": resp.text[:500]}
    except Exception as exc:
        return False, {"status": "error", "detail": str(exc)}


def query_params(**values: Any) -> dict[str, Any]:
    params: dict[str, Any] = {}
    for key, value in values.items():
        if value is None:
            continue
        params[key] = value
    return params
