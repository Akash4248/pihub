"""
Shared Security & Auth Module for PIHUB Ecosystem
Provides authentication, authorization, prompt injection sanitization,
filename path traversal protection, and safe streaming file upload handling.
"""

import os
import re
import secrets
import hmac
import logging
from pathlib import Path
from typing import Optional, AsyncGenerator

from fastapi import HTTPException, Security, Depends, status, Header, UploadFile
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
import jwt

logger = logging.getLogger(__name__)


def _get_or_create_fallback_jwt_secret() -> str:
    env_secret = os.getenv("JWT_SECRET_KEY")
    insecure_defaults = {"change-me", "default-secret-key-change-in-production", "change-me-super-secret-jwt-key-min32chars"}
    if env_secret and env_secret not in insecure_defaults:
        return env_secret

    key_dir = Path(".local_run")
    key_dir.mkdir(parents=True, exist_ok=True)
    key_file = key_dir / "jwt_secret.key"

    if key_file.exists():
        try:
            stored_secret = key_file.read_text(encoding="utf-8").strip()
            if stored_secret and len(stored_secret) >= 32:
                try:
                    os.chmod(str(key_file), 0o600)
                except Exception:
                    pass
                return stored_secret
        except Exception:
            pass

    secret = secrets.token_hex(32)
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        fd = os.open(str(key_file), flags, 0o600)
        with open(fd, "w", encoding="utf-8") as f:
            f.write(secret)
        try:
            os.chmod(str(key_file), 0o600)
        except Exception:
            pass
    except Exception as e:
        logger.warning(f"Could not persist JWT key to {key_file}: {e}")

    return secret


# Fallback auto-generated/persisted random keys if environment variables are not set or default
_GLOBAL_FALLBACK_JWT_SECRET = _get_or_create_fallback_jwt_secret()

_GLOBAL_INTERNAL_SERVICE_KEY = os.getenv("INTERNAL_SERVICE_KEY")
if not _GLOBAL_INTERNAL_SERVICE_KEY or _GLOBAL_INTERNAL_SERVICE_KEY in ["change-me"]:
    _GLOBAL_INTERNAL_SERVICE_KEY = secrets.token_hex(32)
    logger.warning("INTERNAL_SERVICE_KEY auto-generated temporary secure token.")

JWT_ALGORITHM = "HS256"
security_bearer = HTTPBearer(auto_error=False)

def get_jwt_secret_key() -> str:
    secret = os.getenv("JWT_SECRET_KEY")
    insecure_defaults = {"change-me", "default-secret-key-change-in-production", "change-me-super-secret-jwt-key-min32chars"}
    if secret and secret not in insecure_defaults:
        return secret
    return _get_or_create_fallback_jwt_secret()

def get_internal_service_key() -> str:
    key = os.getenv("INTERNAL_SERVICE_KEY", _GLOBAL_INTERNAL_SERVICE_KEY)
    if key in ["change-me"]:
        return _GLOBAL_INTERNAL_SERVICE_KEY
    return key

async def verify_jwt_token(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security_bearer),
) -> dict:
    """
    FastAPI dependency validating JWT Bearer token on guarded endpoints.
    """
    if not credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required. Missing Bearer token.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    token = credentials.credentials
    try:
        payload = jwt.decode(token, get_jwt_secret_key(), algorithms=[JWT_ALGORITHM])
        return payload
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has expired.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except jwt.PyJWTError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid authentication token: {e}",
            headers={"WWW-Authenticate": "Bearer"},
        )

async def verify_internal_api_key(
    x_internal_key: Optional[str] = Header(None, alias="X-Internal-Service-Key"),
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security_bearer),
) -> bool:
    """
    Allows request if valid user JWT token is supplied OR valid internal service key header is supplied.
    This guarantees both external user auth and inter-service communication work cleanly locally and under Docker.
    """
    expected_key = get_internal_service_key()
    if x_internal_key and hmac.compare_digest(x_internal_key, expected_key):
        return True
    
    if credentials:
        try:
            jwt.decode(credentials.credentials, get_jwt_secret_key(), algorithms=[JWT_ALGORITHM])
            return True
        except Exception:
            pass

    # NOTE: ALLOW_ANONYMOUS_DEV intentionally does NOT bypass guarded endpoints.
    # A missing/invalid credential is always rejected here. (The dev escape hatch
    # is confined to read-only requests inside InternalAuthMiddleware.)
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Unauthorized service request. Missing or invalid authentication token.",
    )


# Methods that never mutate state. Used to bound the dev-only anonymous escape.
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# Paths that must stay reachable without credentials (probes, docs, metrics).
PUBLIC_PATH_PREFIXES: tuple[str, ...] = (
    "/health",
    "/healthz",
    "/ready",
    "/readyz",
    "/livez",
    "/ping",
    "/metrics",
    "/docs",
    "/redoc",
    "/openapi.json",
    "/favicon.ico",
)


def _extract_bearer(headers: dict[str, str]) -> str | None:
    auth = headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return None


def request_is_authenticated(headers: dict[str, str]) -> bool:
    """True if the request carries a valid internal service key or a valid JWT."""
    internal_key = headers.get("x-internal-service-key")
    if internal_key and hmac.compare_digest(internal_key, get_internal_service_key()):
        return True
    token = _extract_bearer(headers)
    if token:
        try:
            jwt.decode(token, get_jwt_secret_key(), algorithms=[JWT_ALGORITHM])
            return True
        except jwt.PyJWTError:
            return False
    return False


def _has_admin_role(payload: dict) -> bool:
    roles = {str(payload.get("role", "")).lower()}
    for key in ("roles", "scopes"):
        value = payload.get(key)
        if isinstance(value, (list, tuple)):
            roles.update(str(v).lower() for v in value)
    return bool(roles & {"admin", "superadmin", "owner", "service"})


def _headers_from_scope(scope: dict) -> dict[str, str]:
    return {
        k.decode("latin-1").lower(): v.decode("latin-1")
        for k, v in scope.get("headers", [])
    }


async def _send_json(send, status_code: int, payload: dict) -> None:
    import json

    body = json.dumps(payload).encode("utf-8")
    await send({
        "type": "http.response.start",
        "status": status_code,
        "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())],
    })
    await send({"type": "http.response.body", "body": body})


class InternalAuthMiddleware:
    """Fail-closed ASGI middleware enforcing authentication on every HTTP route.

    A request is allowed when it is:
      * a public path (health probes, docs, metrics), or
      * carrying a valid ``X-Internal-Service-Key`` or Bearer JWT, or
      * a *read-only* request while ``ALLOW_ANONYMOUS_DEV=true`` (dev escape; never
        permits mutations, so nothing can be written or deleted anonymously).

    WebSocket and lifespan scopes pass through untouched.
    """

    def __init__(self, app, public_prefixes: tuple[str, ...] = PUBLIC_PATH_PREFIXES, extra_check=None):
        self.app = app
        self.public_prefixes = tuple(public_prefixes)
        self.extra_check = extra_check

    def _is_public(self, path: str) -> bool:
        for prefix in self.public_prefixes:
            # "/" must be an EXACT match. Using startswith("/") would mark every
            # path public and silently disable authentication for the service.
            if prefix == "/":
                if path == "/":
                    return True
                continue
            if path == prefix or path.startswith(prefix):
                return True
        return False

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        path = scope.get("path", "")
        method = scope.get("method", "GET").upper()
        if self._is_public(path):
            return await self.app(scope, receive, send)

        headers = _headers_from_scope(scope)
        if request_is_authenticated(headers):
            return await self.app(scope, receive, send)

        if self.extra_check is not None and self.extra_check(headers):
            return await self.app(scope, receive, send)

        if method in SAFE_METHODS and os.getenv("ALLOW_ANONYMOUS_DEV", "false").lower() == "true":
            return await self.app(scope, receive, send)

        await _send_json(send, status.HTTP_401_UNAUTHORIZED, {
            "detail": "Unauthorized. Provide a valid Bearer token or X-Internal-Service-Key.",
        })


async def require_admin(
    x_internal_key: Optional[str] = Header(None, alias="X-Internal-Service-Key"),
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security_bearer),
) -> bool:
    """Guards destructive/admin operations.

    Accepts the internal service key (service-to-service) or a JWT whose role is
    administrative. A plain end-user token is NOT sufficient, and the dev escape
    hatch never applies here — so no anonymous caller can delete or reset data.
    """
    if x_internal_key and hmac.compare_digest(x_internal_key, get_internal_service_key()):
        return True
    if credentials:
        try:
            payload = jwt.decode(credentials.credentials, get_jwt_secret_key(), algorithms=[JWT_ALGORITHM])
        except jwt.PyJWTError:
            payload = None
        if payload and _has_admin_role(payload):
            return True
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Administrative privileges required for this operation.",
    )

def sanitize_safe_filename(filename: str) -> str:
    """
    Prevents path traversal vulnerabilities by stripping directory separators and dangerous characters.
    Example: '../../../etc/passwd' -> 'passwd'
    """
    raw_name = Path(filename).name
    cleaned = re.sub(r"[^\w\.-]", "_", raw_name)
    return cleaned if cleaned else "unnamed_upload.bin"

async def read_file_chunks_safely(
    upload_file: UploadFile,
    max_bytes: int = 50 * 1024 * 1024,
    chunk_size: int = 1024 * 1024,
) -> AsyncGenerator[bytes, None]:
    """
    Streams file upload in chunks, enforcing strict byte limits to prevent DoS/OOM attacks.
    """
    total_read = 0
    while True:
        chunk = await upload_file.read(chunk_size)
        if not chunk:
            break
        total_read += len(chunk)
        if total_read > max_bytes:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=f"Uploaded file exceeds maximum allowed limit of {max_bytes // (1024 * 1024)} MB.",
            )
        yield chunk

def validate_pdf_upload(filename: str | None, content: bytes) -> str:
    """
    Sanitize an uploaded filename and verify PDF magic bytes.
    Returns the safe filename. Raises 400 on traversal residue or non-PDF content.
    """
    safe_name = sanitize_safe_filename(filename or "upload.pdf")
    if not content.lstrip().startswith(b"%PDF"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Uploaded file is not a PDF (missing %PDF header).",
        )
    return safe_name

# Single source of truth for prompt-injection tokens. Inference-service and
# gateway both sanitize against this list — keep it in sync in one place.
LLM_INJECTION_TOKENS: tuple[str, ...] = (
    "<|im_start|>", "<|im_end|>", "<|endoftext|>",
    "[INST]", "[/INST]", "<<SYS>>", "<</SYS>>",
    "### Instruction:", "### System:", "### Response:",
    "SYSTEM:", "USER:", "ASSISTANT:",
)

def sanitize_llm_prompt(user_text: str) -> str:
    """
    Sanitizes user prompt inputs to prevent ChatML / LLM system prompt injection attacks.
    NFKC-normalizes, strips control chars, removes injection tokens
    (case-insensitive), and collapses whitespace.
    """
    if not user_text:
        return ""

    import unicodedata

    normalized = unicodedata.normalize("NFKC", user_text)
    cleaned_chars: list[str] = []
    for char in normalized:
        category = unicodedata.category(char)
        if char in {"\n", "\r", "\t"}:
            cleaned_chars.append("\n" if char != "\t" else " ")
            continue
        if category.startswith("C"):
            continue
        cleaned_chars.append(char)

    cleaned = "".join(cleaned_chars)
    for token in LLM_INJECTION_TOKENS:
        cleaned = re.sub(re.escape(token), "", cleaned, flags=re.IGNORECASE)

    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in cleaned.splitlines()]
    return "\n".join(lines).strip()
