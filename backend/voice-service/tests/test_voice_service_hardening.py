import pytest
from fastapi.testclient import TestClient

from app import create_app


def test_voice_service_health_disabled(monkeypatch):
    monkeypatch.setenv("VOICE_SERVICE_ENABLED", "false")
    app = create_app()
    client = TestClient(app)

    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "disabled"
    assert data["voice_service_enabled"] is False
    assert data["capabilities"]["voice_query"] is False
    assert data["capabilities"]["stt"] is False
    assert data["capabilities"]["tts"] is False


def test_voice_service_health_enabled(monkeypatch):
    monkeypatch.setenv("VOICE_SERVICE_ENABLED", "true")
    app = create_app()
    client = TestClient(app)

    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"
    assert data["voice_service_enabled"] is True
    assert data["capabilities"]["voice_query"] is True


def test_voice_routes_return_503_when_disabled(monkeypatch):
    monkeypatch.setenv("VOICE_SERVICE_ENABLED", "false")
    app = create_app()
    client = TestClient(app)

    # POST /voice/query -> 503
    res_query = client.post("/voice/query", json={"query": "Hello", "language": "en"})
    assert res_query.status_code == 503
    assert res_query.json()["detail"]["code"] == "VOICE_SERVICE_DISABLED"

    # POST /voice/tts -> 503
    res_tts = client.post("/voice/tts", json={"text": "Hello", "language": "en"})
    assert res_tts.status_code == 503
    assert res_tts.json()["detail"]["code"] == "VOICE_SERVICE_DISABLED"

    # POST /voice/stt -> 503
    res_stt = client.post("/voice/stt", files={"file": ("test.wav", b"dummy wav data", "audio/wav")})
    assert res_stt.status_code == 503
    assert res_stt.json()["detail"]["code"] == "VOICE_SERVICE_DISABLED"


def test_voice_websocket_disabled(monkeypatch):
    monkeypatch.setenv("VOICE_SERVICE_ENABLED", "false")
    app = create_app()
    client = TestClient(app)

    with client.websocket_connect("/voice/stream") as websocket:
        data = websocket.receive_json()
        assert data["type"] == "error"
        assert "disabled" in data["message"].lower()
