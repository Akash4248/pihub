"""Contract hardening: slug parity, benchmark scoping, retrieval version gate."""
from __future__ import annotations

import re
from types import SimpleNamespace

from shared.pack_version import RETRIEVAL_INDEX_VERSION
from shared.text_normalization import normalize_curriculum_name

from app.pack_storage.storage_layout import StorageLayout
from app.validation.retrieval_validator import RetrievalValidator


def test_slugify_matches_pack_id_parts():
    # StorageLayout.slugify must equal PackGenerator._pack_id_part for the
    # same input (normalize + [^a-z0-9]+ -> _). pack_generator itself needs
    # qdrant_client at import, so replicate its two-line formula here.
    def pack_id_part(value):
        normalized = normalize_curriculum_name(value)
        return re.sub(r"[^a-z0-9]+", "_", normalized).strip("_") or "unknown"

    for raw in ["Social Science", "Maths", "Light - Reflection", "Environmental Studies"]:
        assert StorageLayout.slugify(raw) == pack_id_part(raw), raw


def test_pack_directory_keeps_canonical_pack_id_verbatim():
    dirname = StorageLayout.pack_directory_name(7, "Science", "Nutrition", "class_7_science_en")
    assert dirname.endswith("/class_7_science_en"), dirname


def test_benchmark_scoped_to_requested_pack():
    from fastapi import HTTPException

    from app.api import pack_routes

    record = {"pack_id": "p1", "version": "1.0.0"}
    other = {"pack_id": "p2", "version": "1.0.0"}
    seen: dict = {}

    class FakeRunner:
        def run(self, packs):
            seen["packs"] = packs

            class R:
                def model_dump(self):
                    return {"packs": [p["pack_id"] for p in packs]}

            return R()

    class FakeRepo:
        def get_pack(self, pack_id, version=None):
            return record if pack_id == "p1" else None

        def list_packs(self):
            return [record, other]

    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(pack_repository=FakeRepo(), retrieval_benchmark=FakeRunner())
        )
    )
    out = pack_routes.benchmark_pack("p1", request)
    assert out == {"packs": ["p1"]}
    assert seen["packs"] == [record]

    try:
        pack_routes.benchmark_pack("missing", request)
    except HTTPException as exc:
        assert exc.status_code == 404
    else:
        raise AssertionError("expected 404 for unknown pack")


def _manifest(version):
    return {"pack_id": "p1", "retrieval_index_version": version}


def test_retrieval_version_gate():
    validator = RetrievalValidator()
    artifacts = {"retrieval_index": {"vectors": 1}}

    ok, errors, warnings = validator.validate(_manifest(RETRIEVAL_INDEX_VERSION), artifacts)
    assert ok and not errors and not warnings

    ok, errors, warnings = validator.validate(_manifest("semantic-v1"), artifacts)
    assert ok and not errors
    assert any("legacy" in w for w in warnings)

    ok, errors, warnings = validator.validate(_manifest("v9"), artifacts)
    assert not ok
    assert any("unexpected" in e for e in errors)

    ok, errors, warnings = validator.validate({"pack_id": "p1"}, artifacts)
    assert not ok
    assert any("missing" in e for e in errors)
