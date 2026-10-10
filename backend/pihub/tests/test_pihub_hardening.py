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


def test_create_pack_rejects_bad_metadata_json():
    client = TestClient(app)
    resp = client.post(
        "/packs",
        headers={"x-pihub-token": settings.admin_token},
        files={"file": ("pack.zip", b"PK\x03\x04" + b"0" * 64, "application/zip")},
        data={"pack_name": "p1", "metadata": "{not-json"},
    )
    assert resp.status_code == 400
    assert "metadata" in resp.json()["detail"].lower()


def test_create_pack_rejects_non_zip():
    client = TestClient(app)
    resp = client.post(
        "/packs",
        headers={"x-pihub-token": settings.admin_token},
        files={"file": ("evil.exe", b"MZ" + b"0" * 64, "application/octet-stream")},
        data={"pack_name": "p1", "metadata": "{}"},
    )
    assert resp.status_code == 400


def test_atomic_write_json_roundtrip():
    from shared.file_utils import atomic_write_json
    import json

    with tempfile.TemporaryDirectory() as tmp_dir:
        target = Path(tmp_dir) / "state.json"
        atomic_write_json(target, {"a": 1})
        assert json.loads(target.read_text()) == {"a": 1}
        atomic_write_json(target, {"a": 2})
        assert json.loads(target.read_text()) == {"a": 2}
        leftovers = list(Path(tmp_dir).glob("*.part"))
        assert leftovers == []


def test_legacy_auth_token_column_migrated_to_hash():
    import sqlite3

    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "legacy.sqlite3"
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "create table devices (device_id text primary key, device_name text not null,"
            " role text not null, classroom text, auth_token text, status text not null,"
            " metadata text not null, created_at integer not null, updated_at integer not null,"
            " last_seen integer)"
        )
        conn.execute(
            "insert into devices values ('d1', 'Tablet', 'student', 'R1', 'plain-secret',"
            " 'online', '{}', 1, 1, 1)"
        )
        conn.commit()
        conn.close()

        migrated = PiHubStore(str(db_path))
        cols = [
            r[1]
            for r in __import__("sqlite3").connect(str(db_path)).execute("pragma table_info(devices)").fetchall()
        ]
        assert "auth_token" not in cols
        assert "auth_token_hash" in cols
        assert migrated.verify_device_token("d1", "plain-secret") is True
        assert migrated.verify_device_token("d1", "wrong") is False
        device = migrated.get_device("d1")
        assert "auth_token" not in device


def test_host_url_defaults_to_hotspot_gateway(monkeypatch):
    monkeypatch.delenv("HOST_URL", raising=False)
    from shared.config import Settings

    assert Settings().host_url == "http://192.168.42.1:8000"


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
