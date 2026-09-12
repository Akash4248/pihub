import io
import pytest
from fastapi.testclient import TestClient

from app.main import app
from shared.security import get_internal_service_key

client = TestClient(app)


def test_upload_without_token_returns_401():
    response = client.post(
        "/upload",
        files={"file": ("test.pdf", b"%PDF-1.4...", "application/pdf")},
    )
    assert response.status_code == 401
    assert "Unauthorized" in response.json()["detail"] or "Authentication" in response.json()["detail"]


def test_upload_with_internal_key_passes_auth_gate():
    internal_key = get_internal_service_key()

    class MockResponse:
        is_error = False
        is_success = True
        status_code = 200
        headers = {"content-type": "application/json"}

        def json(self):
            return {
                "file_name": "test.pdf",
                "chunks_created": 1,
                "collection": "educational_chunks_bge_m3",
                "metadata": {"grade": 10},
            }

    class MockHttp:
        async def request(self, method, url, **kwargs):
            return MockResponse()

        async def post(self, url, **kwargs):
            return MockResponse()

        async def get(self, url, **kwargs):
            return MockResponse()

    app.state.http = MockHttp()

    response = client.post(
        "/upload",
        headers={"X-Internal-Service-Key": internal_key},
        files={"file": ("test.pdf", b"%PDF-1.4 test content", "application/pdf")},
    )
    assert response.status_code == 200
    assert response.json()["file_name"] == "test.pdf"


def test_upload_exceeding_50mb_returns_413():
    internal_key = get_internal_service_key()
    large_chunk = b"X" * (51 * 1024 * 1024)  # 51 MB
    response = client.post(
        "/upload",
        headers={"X-Internal-Service-Key": internal_key},
        files={"file": ("large.pdf", large_chunk, "application/pdf")},
    )
    assert response.status_code == 413
    assert "exceeds maximum allowed limit" in response.json()["detail"]


def test_packs_sync_grade_filter_returns_empty_when_no_match():
    class MockResponse:
        is_success = True
        is_error = False
        status_code = 200
        headers = {"content-type": "application/json"}

        def json(self):
            return {"packs": [{"pack_id": "p10", "grade": 10, "subject": "Science"}]}

    class MockHttp:
        async def request(self, method, url, **kwargs):
            return MockResponse()

        async def get(self, url, **kwargs):
            return MockResponse()

    app.state.http = MockHttp()

    response = client.get("/packs/sync?grade=99")
    assert response.status_code == 200
    data = response.json()
    assert data["changed_packs"] == []
    assert data["asset_updates"] == []


def test_debug_routes_require_internal_key():
    response = client.get("/debug/curriculum")
    assert response.status_code == 401

    response = client.get("/debug/metadata?path=/tmp")
    assert response.status_code == 401
