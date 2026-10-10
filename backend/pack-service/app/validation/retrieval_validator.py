from __future__ import annotations

from typing import Any

from shared.pack_version import LEGACY_RETRIEVAL_INDEX_VERSIONS, RETRIEVAL_INDEX_VERSION


class RetrievalValidator:
    def validate(self, manifest: dict[str, Any], artifacts: dict[str, Any]) -> tuple[bool, list[str], list[str]]:
        errors: list[str] = []
        warnings: list[str] = []
        retrieval_index_version = manifest.get("retrieval_index_version")
        if not retrieval_index_version:
            errors.append("retrieval_index_version:missing")
        elif retrieval_index_version in LEGACY_RETRIEVAL_INDEX_VERSIONS:
            warnings.append(f"retrieval_index_version:legacy:{retrieval_index_version}")
        elif retrieval_index_version != RETRIEVAL_INDEX_VERSION:
            errors.append(f"retrieval_index_version:unexpected:{retrieval_index_version}")

        retrieval_index = artifacts.get("retrieval_index", {})
        if not isinstance(retrieval_index, dict):
            errors.append("retrieval_index:not-a-dict")
        elif not retrieval_index:
            errors.append("retrieval_index:empty")

        return not errors, errors, warnings
