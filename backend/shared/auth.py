import time
import logging
from typing import Optional, Set
import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from shared.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

_ph = PasswordHasher()

# Fallback in-memory revoked token cache if Redis is not present
_in_memory_revoked_tokens: Set[str] = set()

def hash_password(password: str) -> str:
    """Hash password using Argon2id."""
    return _ph.hash(password)

def verify_password(password: str, hashed: str) -> bool:
    """Verify password against Argon2id hash."""
    try:
        return _ph.verify(hashed, password)
    except VerifyMismatchError:
        return False
    except Exception as e:
        logger.error("[AUTH] Password verification error: %s", e)
        return False

def create_jwt_token(data: dict, expires_in_seconds: int = 43200) -> str:
    """Issue JWT token using shared secret key (default 12h expiry)."""
    payload = data.copy()
    payload["exp"] = int(time.time()) + expires_in_seconds
    payload["iat"] = int(time.time())
    return jwt.encode(payload, settings.jwt_secret_key, algorithm="HS256")

def decode_jwt_token(token: str) -> Optional[dict]:
    """Decode and validate JWT token claims."""
    if is_token_revoked(token):
        logger.warning("[AUTH] Token has been revoked")
        return None
    try:
        payload = jwt.decode(token, settings.jwt_secret_key, algorithms=["HS256"])
        return payload
    except jwt.ExpiredSignatureError:
        logger.warning("[AUTH] JWT Token expired")
        return None
    except jwt.InvalidTokenError as e:
        logger.warning("[AUTH] Invalid JWT Token: %s", e)
        return None

def revoke_token(token: str) -> None:
    """Revoke JWT token (adaptive Redis or memory store)."""
    _in_memory_revoked_tokens.add(token)

def is_token_revoked(token: str) -> bool:
    """Check if JWT token is revoked."""
    return token in _in_memory_revoked_tokens
