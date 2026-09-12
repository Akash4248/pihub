import pytest
import asyncio
from app.pack_generator import PackGenerator, PackQualityGateError, PackGenerationNoContentError
from app.pack_system.manifest_validator import ManifestValidator
from app.pack_system.version_manager import VersionManager
from app.pack_storage.pack_registry import PackRegistry
from pathlib import Path
import tempfile


def test_manifest_validator_allows_language_pack_with_null_subject():
    validator = ManifestValidator()
    manifest = {
        "pack_id": "lang_kn_5",
        "version": "1.0.0",
        "grade": 5,
        "subject": None,
        "chapter": None,
        "language": "kn",
        "generated_at": "2026-09-12T00:00:00Z",
        "checksum": "abc",
        "retrieval_index_version": "v2",
        "artifact_counts": {"content": 10},
        "generation_metadata": {"pack_type": "language"},
    }
    # Calculate valid checksum for test
    from app.pack_system.checksum_generator import ChecksumGenerator
    checksum_source = {k: v for k, v in manifest.items() if k != "checksum"}
    manifest["checksum"] = ChecksumGenerator.checksum_dict(checksum_source)

    valid, errors = validator.validate(manifest)
    assert valid is True
    assert errors == []


def test_version_manager_loose_parse_and_search():
    with tempfile.TemporaryDirectory() as tmp_dir:
        registry = PackRegistry(Path(tmp_dir))
        manifest = {
            "pack_id": "class5_science_en",
            "version": "1.0.0",
            "grade": 5,
            "subject": "science",
            "chapter": None,
            "language": "en",
            "checksum": "123",
            "artifact_counts": {},
            "generation_metadata": {},
        }
        registry.register(manifest, "/tmp/dir", "/tmp/archive.tar.gz")

        res_exact = registry.search(version="1.0.0")
        assert len(res_exact) == 1

        res_loose = registry.search(version="1.0")
        assert len(res_loose) == 1
        assert res_loose[0]["pack_id"] == "class5_science_en"


def test_regional_language_fallback_fails_quality_gate():
    generator = PackGenerator(
        qdrant_url="http://localhost:6333",
        qdrant_collection="test",
        pack_storage_path="/tmp/packs",
        curriculum_graph_path="/tmp/graph.json",
    )

    chunks = [{"chunk_id": "c1", "text": "English chunk content", "metadata": {"grade": 5}}]
    metadata = {
        "grade": 5,
        "subject": "science",
        "language": "kn",
        "used_fallback": "remove_language_filter",
    }

    with pytest.raises(PackQualityGateError) as exc_info:
        asyncio.run(generator._create_pack("lang_kn_5", "language", chunks, metadata))

    assert "Regional language pack (kn) publication blocked" in str(exc_info.value)


def test_english_language_fallback_allowed():
    generator = PackGenerator(
        qdrant_url="http://localhost:6333",
        qdrant_collection="test",
        pack_storage_path="/tmp/packs",
        curriculum_graph_path="/tmp/graph.json",
    )

    chunks = [{"chunk_id": "c1", "text": "English chunk content text with enough words for pipeline validation", "metadata": {"grade": 5}}]
    metadata = {
        "grade": 5,
        "subject": "science",
        "language": "en",
        "used_fallback": "remove_language_filter",
    }

    # Should not raise PackQualityGateError for language fallback on 'en'
    try:
        asyncio.run(generator._create_pack("class5_science_en", "class", chunks, metadata))
    except PackQualityGateError as e:
        assert "Regional language pack" not in str(e)
