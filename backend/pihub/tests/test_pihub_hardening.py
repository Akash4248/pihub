import pytest
import hmac
import hashlib
import tempfile
from pathlib import Path
from fastapi import HTTPException
from fastapi.testclient import TestClient
from api.main import app, settings, issue_device_token, require_admin
from cache.store import PiHubStore
from deployment.hotspot import Hostapd


def test_require_admin_timing_safe_check():
    # Valid token passes
    require_admin(x_pihub_token=settings.admin_token)

    # Invalid token raises 401
    with pytest.raises(HTTPException) as exc_info:
        require_admin(x_pihub_token="wrong-token")
    assert exc_info.value.status_code == 401

    # Missing token raises 401
    with pytest.raises(HTTPException) as exc_info:
        require_admin(x_pihub_token=None)
    assert exc_info.value.status_code == 401


def test_issue_device_token_hmac():
    token1 = issue_device_token("seed-123")
    token2 = issue_device_token("seed-123")
    assert token1 == token2

    expected = hmac.new(
        settings.device_token_secret.encode("utf-8"),
        b"seed-123",
        hashlib.sha256,
    ).hexdigest()
    assert token1 == expected


def test_hostapd_sanitizes_newline_injection():
    hostapd = Hostapd(interface="wlan0")

    # Valid config
    conf = hostapd.generate_config("Classroom_WiFi", "secretpass123")
    assert "ssid=Classroom_WiFi" in conf
    assert "wpa_passphrase=secretpass123" in conf

    # Injected newlines are stripped, preventing newline injection
    conf_injected = hostapd.generate_config("Classroom\ninjected=1", "secretpass123")
    assert "ssid=Classroominjected=1" in conf_injected
    assert "\ninjected=1" not in conf_injected

    # Empty clean input raises ValueError
    with pytest.raises(ValueError) as exc_info:
        hostapd.generate_config("\n\r", "secretpass123")
    assert "cannot contain newlines" in str(exc_info.value)


def test_student_device_map_sqlite():
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "test_store.sqlite3"
        store = PiHubStore(str(db_path))

        store.map_student_device("dev-001", "student-abc")
        mapped_student = store.get_student_for_device("dev-001")
        mapped_device = store.get_device_for_student("student-abc")

        assert mapped_student == "student-abc"
        assert mapped_device == "dev-001"

        mappings = store.list_student_device_mappings()
        assert len(mappings) == 1
        assert mappings[0]["device_id"] == "dev-001"
        assert mappings[0]["student_id"] == "student-abc"


def test_device_status_strips_auth_token():
    client = TestClient(app)

    # Register device
    reg_resp = client.post("/devices", json={"device_name": "Tablet-1", "role": "student", "classroom": "Room101"})
    assert reg_resp.status_code == 200
    device_id = reg_resp.json()["device_id"]

    # Check status endpoint
    status_resp = client.get(f"/devices/{device_id}/status")
    assert status_resp.status_code == 200
    body = status_resp.json()
    assert "device" in body
    assert "auth_token" not in body["device"]
