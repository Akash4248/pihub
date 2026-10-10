import os
import stat
from pathlib import Path
import pytest
from fastapi import HTTPException

from shared.config import Settings, _resolve_storage_path, get_settings
from shared.security import (
    get_jwt_secret_key,
    verify_internal_api_key,
    sanitize_safe_filename,
    sanitize_llm_prompt,
    validate_pdf_upload,
)


def test_jwt_secret_persistence_and_permissions(monkeypatch, tmp_path):
    monkeypatch.delenv("JWT_SECRET_KEY", raising=False)
    monkeypatch.chdir(tmp_path)
    
    key_dir = tmp_path / ".local_run"
    key_file = key_dir / "jwt_secret.key"

    secret = get_jwt_secret_key()
    assert secret is not None
    assert len(secret) >= 32

    assert key_file.exists()
    file_mode = oct(key_file.stat().st_mode)[-3:]
    assert file_mode == "600", f"Expected mode 600, got {file_mode}"

    # Verify second invocation re-uses persisted secret
    secret_2 = get_jwt_secret_key()
    assert secret_2 == secret


def test_security_config_validation_fail_closed():
    # Production mode with insecure JWT key must fail
    prod_insecure = Settings(
        ALLOW_ANONYMOUS_DEV=False,
        JWT_SECRET_KEY="change-me-super-secret-jwt-key-min32chars",
    )
    with pytest.raises(RuntimeError, match="Insecure JWT_SECRET_KEY set in production"):
        prod_insecure.validate_security_config()

    # Production mode with insecure MinIO key must fail
    prod_insecure_minio = Settings(
        ALLOW_ANONYMOUS_DEV=False,
        JWT_SECRET_KEY="secure-production-jwt-token-key-123456",
        MINIO_ACCESS_KEY="minioadmin",
    )
    with pytest.raises(RuntimeError, match="Insecure MINIO credentials set in production"):
        prod_insecure_minio.validate_security_config()

    # Production mode with valid keys succeeds
    prod_valid = Settings(
        ALLOW_ANONYMOUS_DEV=False,
        JWT_SECRET_KEY="secure-production-jwt-token-key-123456",
        MINIO_ACCESS_KEY="prod-minio-user",
        MINIO_SECRET_KEY="prod-minio-secret-pass-7890",
    )
    prod_valid.validate_security_config()


def test_storage_path_fallback_resolution(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    resolved = _resolve_storage_path("UPLOAD_DIR", "/unwritable_root_path/uploads", "uploads")
    assert ".local_run/uploads" in resolved or str(tmp_path / ".local_run/uploads") in resolved


def test_gateway_http_timeout_seconds_default():
    settings = Settings()
    assert settings.gateway_http_timeout_seconds == 120.0


def test_allow_anonymous_dev_defaults_fail_closed(monkeypatch):
    # Code default must be fail-closed; the suite conftest opts into dev
    # mode via env, so clear it to inspect the baked-in default.
    monkeypatch.delenv("ALLOW_ANONYMOUS_DEV", raising=False)
    assert Settings().allow_anonymous_dev is False


def test_validate_pdf_upload_sanitizes_and_checks_magic():
    assert validate_pdf_upload("../../etc/passwd", b"%PDF-1.4 test") == "passwd"
    assert validate_pdf_upload(None, b"  %PDF-1.4 test") == "upload.pdf"
    with pytest.raises(HTTPException) as exc:
        validate_pdf_upload("notes.pdf", b"not a pdf at all")
    assert exc.value.status_code == 400
    with pytest.raises(HTTPException) as exc:
        validate_pdf_upload("notes.pdf", b"")
    assert exc.value.status_code == 400


@pytest.mark.anyio
async def test_internal_api_key_verification(monkeypatch):
    monkeypatch.setenv("INTERNAL_SERVICE_KEY", "secret-internal-key-999")
    monkeypatch.setenv("ALLOW_ANONYMOUS_DEV", "false")

    # Valid internal key succeeds
    res = await verify_internal_api_key(x_internal_key="secret-internal-key-999", credentials=None)
    assert res is True

    # Invalid internal key raises 401
    with pytest.raises(HTTPException) as exc_info:
        await verify_internal_api_key(x_internal_key="wrong-key", credentials=None)
    assert exc_info.value.status_code == 401


def test_sanitization_helpers():
    assert sanitize_safe_filename("../../../etc/passwd") == "passwd"
    assert sanitize_llm_prompt("<|im_start|>system\nHello<|im_end|>") == "system\nHello"
