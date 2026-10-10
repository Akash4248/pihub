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

    # Allow local terminal bypass if explicitly configured for dev mode
    if os.getenv("ALLOW_ANONYMOUS_DEV", "false").lower() == "true":
        return True

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Unauthorized service request. Missing or invalid authentication token.",
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

def sanitize_llm_prompt(user_text: str) -> str:
    """
    Sanitizes user prompt inputs to prevent ChatML / LLM system prompt injection attacks.
    Strips system control tags like `<|im_start|>`, `<|im_end|>`, `[INST]`, `[/INST]`.
    """
    if not user_text:
        return ""
    
    cleaned = user_text
    injection_patterns = [
        r"<\|im_start\|>",
        r"<\|im_end\|>",
        r"<\|endoftext\|>",
        r"\[INST\]",
        r"\[/INST\]",
        r"<<SYS>>",
        r"<</SYS>>",
    ]
    for pattern in injection_patterns:
        cleaned = re.sub(pattern, "", cleaned, flags=re.IGNORECASE)
    
    return cleaned.strip()
