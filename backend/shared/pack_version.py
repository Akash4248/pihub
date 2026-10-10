"""Canonical pack contract versions shared by pack-service and content-pipeline."""

#: Manifest-level retrieval index version emitted by ManifestBuilder.
RETRIEVAL_INDEX_VERSION = "v2"

#: Older versions still accepted with a warning (never emitted today, but may
#: exist in packs built before the contract was centralized).
LEGACY_RETRIEVAL_INDEX_VERSIONS = frozenset({"semantic-v1"})
