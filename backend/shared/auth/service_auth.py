from __future__ import annotations

import logging
import time
from typing import Any

from fastapi import Depends, Header, HTTPException, status
import jwt

from shared.config import get_settings
from shared.security import get_internal_service_key

logger = logging.getLogger(__name__)

# Service Permissions Matrix (Zero-Trust Principle)
SERVICE_PERMISSIONS: dict[str, list[str]] = {
    "gateway": [
        "service:call",
        "inference:generate",
        "pack:create",
        "pack:read",
        "pipeline:index",
        "pihub:sync",
        "pihub:admin",
    ],
    "pack-service": [
        "service:call",
        "inference:generate",
        "pipeline:artifacts",
        "pihub:register_pack",
        "events:publish",
    ],
    "content-pipeline": [
        "service:call",
        "qdrant:write",
        "mongo:write",
        "events:publish",
    ],
    "pihub": [
        "service:call",
        "pack:read",
        "events:publish",
        "sync:manage",
    ],
    "inference-service": [
        "service:call",
    ],
    "experiment-service": [
        "service:call",
        "inference:generate",
    ],
}


def get_service_secret() -> str:
    settings = get_settings()
    return settings.service_jwt_secret or get_internal_service_key()


def issue_service_token(
    issuer: str,
    audience: str = "internal-services",
    permissions: list[str] | None = None,
    ttl_seconds: int = 3600,
) -> str:
    """Issue a signed JWT for inter-service zero-trust identity."""
    secret = get_service_secret()
    now = int(time.time())
    assigned_perms = permissions or SERVICE_PERMISSIONS.get(issuer, ["service:call"])

    payload = {
        "iss": issuer,
        "sub": issuer,
        "aud": audience,
        "iat": now,
        "exp": now + ttl_seconds,
        "permissions": assigned_perms,
    }
    return jwt.encode(payload, secret, algorithm="HS256")


def verify_service_token(
    token: str,
    required_permission: str | None = None,
    allowed_issuers: list[str] | None = None,
) -> dict[str, Any]:
    """Verify signed service token and enforce permission checks."""
    secret = get_service_secret()
    try:
        claims = jwt.decode(
            token,
            secret,
            algorithms=["HS256"],
            options={"verify_aud": False},
        )
    except jwt.PyJWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid service token: {exc}",
        ) from exc

    issuer = claims.get("iss", "")
    if allowed_issuers and issuer not in allowed_issuers:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Service '{issuer}' is not authorized for this resource.",
        )

    if required_permission:
        perms = claims.get("permissions", [])
        if required_permission not in perms and "service:admin" not in perms:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Service '{issuer}' lacks required permission: {required_permission}",
            )

    return claims


def require_service_permission(
    required_permission: str | None = None,
    allowed_issuers: list[str] | None = None,
):
    """FastAPI Dependency for validating service-to-service tokens."""

    async def _dependency(
        authorization: str | None = Header(None, alias="Authorization"),
        x_service_token: str | None = Header(None, alias="X-Service-Token"),
        x_internal_key: str | None = Header(None, alias="X-Internal-Service-Key"),
    ) -> dict[str, Any]:
        settings = get_settings()

        token = x_service_token
        if not token and authorization and authorization.startswith("Bearer "):
            token = authorization[7:].strip()

        # Backward compatibility with existing internal service key
        if not token and x_internal_key:
            expected_key = get_internal_service_key()
            if x_internal_key == expected_key:
                return {
                    "iss": "internal-mesh",
                    "permissions": ["service:admin", "service:call"],
                }

        # Dev mode bypass if enabled
        if not token and settings.allow_anonymous_dev:
            return {"iss": "dev-anonymous", "permissions": ["service:admin"]}

        if not token:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Missing service authentication token (X-Service-Token or Bearer)",
            )

        return verify_service_token(token, required_permission, allowed_issuers)

    return _dependency
