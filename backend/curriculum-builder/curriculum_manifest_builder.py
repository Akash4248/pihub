"""Build master curriculum manifest from scanner results."""

import hashlib
import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


_FAMILY_ENRICHMENT: dict[str, dict[str, list[str]]] = {
    "statistics": {
        "concepts": ["mean", "median", "mode", "average", "frequency", "dataset", "graph"],
        "glossary_terms": ["mean", "median", "mode", "average", "frequency", "dataset", "data handling", "graph"],
        "synonyms": ["data handling and presentation", "data handling", "data presentation", "statistics"],
        "topic_aliases": ["statistics", "data handling", "data handling and presentation", "data presentation"],
        "educational_keywords": ["mean", "median", "mode", "average", "dataset", "frequency", "data", "graph"],
    },
    "arithmetic progressions": {
        "concepts": ["common difference", "nth term", "sequence", "ap", "progression"],
        "glossary_terms": ["common difference", "nth term", "sequence", "arithmetic progression", "ap"],
        "synonyms": ["arithmetic progression", "arithmetic progressions", "ap", "sequence"],
        "topic_aliases": ["arithmetic progression", "arithmetic progressions", "sequence"],
        "educational_keywords": ["common difference", "nth term", "sequence", "ap", "progression"],
    },
    "triangles": {
        "concepts": ["similar triangles", "congruent triangles", "aaa", "sas", "sss"],
        "glossary_terms": ["similar triangles", "congruent triangles", "aaa", "sas", "sss"],
        "synonyms": ["triangles", "similarity", "congruence"],
        "topic_aliases": ["similar triangles", "congruent triangles"],
        "educational_keywords": ["similar triangles", "congruent triangles", "aaa", "sas", "sss"],
    },
    "surface areas and volumes": {
        "concepts": ["cylinder", "cone", "sphere", "hemisphere", "surface area", "volume"],
        "glossary_terms": ["cylinder", "cone", "sphere", "hemisphere", "surface area", "volume"],
        "synonyms": ["surface areas and volumes", "solid shapes", "mensuration"],
        "topic_aliases": ["surface area", "volume", "solid shapes"],
        "educational_keywords": ["cylinder", "cone", "sphere", "hemisphere", "surface area", "volume"],
    },
}


def _normalize_term(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9\u0900-\u097F\u0C80-\u0CFF]+", " ", str(value).lower())).strip()


def _extract_terms(text: str, limit: int = 16) -> list[str]:
    cleaned = _normalize_term(text)
    if not cleaned:
        return []
    tokens = [token for token in re.findall(r"[\w\u0900-\u097F\u0C80-\u0CFF']+", cleaned) if token]
    fragments: list[str] = []
    for size in (3, 2, 1):
        for index in range(0, max(len(tokens) - size + 1, 0)):
            fragment = " ".join(tokens[index : index + size]).strip()
            if fragment and fragment not in fragments:
                fragments.append(fragment)
            if len(fragments) >= limit:
                return fragments[:limit]
    return fragments[:limit]


def _build_chapter_enrichment(chapter_name: str, subject: str | None = None, description: str = "", topics: list[str] | None = None, concepts: list[str] | None = None, learning_outcomes: list[str] | None = None) -> dict[str, list[str]]:
    topics = topics or []
    concepts = concepts or []
    learning_outcomes = learning_outcomes or []
    base_text = " ".join([chapter_name, subject or "", description, " ".join(topics), " ".join(concepts), " ".join(learning_outcomes)])
    normalized = _normalize_term(base_text)

    family: str | None = None
    families = {
        "statistics": ["statistics", "data handling", "data handling and presentation", "data presentation", "frequency", "mean", "median", "mode", "dataset"],
        "arithmetic progressions": ["arithmetic progression", "arithmetic progressions", "common difference", "nth term", "sequence", "ap"],
        "triangles": ["triangles", "similar triangles", "congruent triangles", "aaa", "sas", "sss"],
        "surface areas and volumes": ["surface areas and volumes", "cylinder", "cone", "sphere", "hemisphere", "surface area", "volume"],
    }
    for name, markers in families.items():
        if any(marker in normalized for marker in markers):
            family = name
            break

    enrichment = {"concepts": [], "glossary_terms": [], "synonyms": [], "topic_aliases": [], "educational_keywords": []}
    if family and family in _FAMILY_ENRICHMENT:
        for key, values in _FAMILY_ENRICHMENT[family].items():
            enrichment[key].extend(values)

    enrichment["concepts"].extend(concepts)
    enrichment["glossary_terms"].extend(concepts)
    enrichment["topic_aliases"].extend(topics)
    enrichment["educational_keywords"].extend(_extract_terms(base_text))
    enrichment["synonyms"].extend(_extract_terms(chapter_name + " " + (subject or "") + " " + description))

    for key, values in enrichment.items():
        seen: set[str] = set()
        normalized_values: list[str] = []
        for value in values:
            term = _normalize_term(value)
            if not term or term in seen:
                continue
            seen.add(term)
            normalized_values.append(term)
        enrichment[key] = normalized_values

    return enrichment


class CurriculumManifestBuilder:
    """Build master curriculum manifest from scanner data."""

    @staticmethod
    def create_manifest_from_scan(scan_data: Dict, version: str = "1.0.0") -> Dict:
        """
        Create master curriculum manifest from scan data.

        Args:
            scan_data: Output from CurriculumScanner.scan()
            version: Curriculum version

        Returns:
            Master curriculum manifest
        """
        manifest = {
            "metadata": {
                "version": version,
                "created_at": datetime.utcnow().isoformat(),
                "total_grades": len(scan_data["metadata"]["grades"]),
                "total_subjects": len(scan_data["metadata"]["subjects"]),
                "total_languages": len(scan_data["metadata"]["languages"]),
                "total_chapters": scan_data["metadata"]["total_pdfs"],
                "grades": scan_data["metadata"]["grades"],
                "subjects": scan_data["metadata"]["subjects"],
                "languages": scan_data["metadata"]["languages"],
            },
            "curriculum_index": {},
        }

        # Build curriculum index
        for key, entry in scan_data["curriculum"].items():
            grade = entry["grade"]
            subject = entry["subject"]
            language = entry["language"]
            part = entry.get("part")
            part_segment = f"_part{part}" if part else ""

            # Create curriculum entry
            curriculum_entry = {
                "grade": grade,
                "subject": subject,
                "language": language,
                "part": part,
                "chapters": [],
            }

            # Add chapters with chapter index
            for idx, chapter in enumerate(entry["chapters"]):
                enrichment = _build_chapter_enrichment(
                    chapter["chapter"],
                    subject=subject,
                    description=chapter.get("description", ""),
                    topics=chapter.get("topics", []),
                    concepts=chapter.get("concepts", []),
                    learning_outcomes=chapter.get("learning_outcomes", []),
                )
                chapter_entry = {
                    "chapter_id": f"{grade}_{subject}_{language}{part_segment}_ch{idx:03d}",
                    "chapter_name": chapter["chapter"],
                    "chapter_number": chapter.get("chapter_number"),
                    "kind": chapter.get("kind", "chapter"),
                    "filename": chapter["filename"],
                    "relative_path": chapter["relative_path"],
                    "part": chapter["part"],
                    "sequence": idx,
                    "metadata": {
                        "enrichment": enrichment,
                        "concepts": enrichment["concepts"],
                        "glossary_terms": enrichment["glossary_terms"],
                        "synonyms": enrichment["synonyms"],
                        "topic_aliases": enrichment["topic_aliases"],
                        "educational_keywords": enrichment["educational_keywords"],
                    },
                }

                curriculum_entry["chapters"].append(chapter_entry)

            manifest["curriculum_index"][key] = curriculum_entry

        # Create curriculum graph (relationships)
        manifest["curriculum_graph"] = CurriculumManifestBuilder._create_curriculum_graph(scan_data)

        return manifest

    @staticmethod
    def _create_curriculum_graph(scan_data: Dict) -> Dict:
        """
        Create curriculum relationships and dependencies.

        Args:
            scan_data: Output from CurriculumScanner.scan()

        Returns:
            Curriculum graph with relationships
        """
        graph = {
            "by_grade": {},
            "by_subject": {},
            "by_language": {},
        }

        # Index by grade
        for key, entry in scan_data["curriculum"].items():
            grade = entry["grade"]
            subject = entry["subject"]

            if grade not in graph["by_grade"]:
                graph["by_grade"][grade] = []

            graph["by_grade"][grade].append({"subject": subject, "key": key})

        # Index by subject
        for key, entry in scan_data["curriculum"].items():
            subject = entry["subject"]
            grade = entry["grade"]

            if subject not in graph["by_subject"]:
                graph["by_subject"][subject] = []

            graph["by_subject"][subject].append({"grade": grade, "key": key})

        # Index by language
        for key, entry in scan_data["curriculum"].items():
            language = entry["language"]

            if language not in graph["by_language"]:
                graph["by_language"][language] = []

            graph["by_language"][language].append({"key": key})

        return graph

    @staticmethod
    def add_chapter_metadata(manifest: Dict, chapter_id: str, metadata: Dict) -> Dict:
        """
        Add enriched metadata to a chapter.

        Args:
            manifest: Master curriculum manifest
            chapter_id: Chapter ID
            metadata: Metadata to add (e.g., difficulty, learning_objectives, keywords)

        Returns:
            Updated manifest
        """
        # Find and update chapter
        for key, curriculum in manifest["curriculum_index"].items():
            for chapter in curriculum["chapters"]:
                if chapter["chapter_id"] == chapter_id:
                    if "metadata" not in chapter:
                        chapter["metadata"] = {}
                    chapter["metadata"].update(metadata)
                    return manifest

        logger.warning(f"Chapter {chapter_id} not found in manifest")
        return manifest

    @staticmethod
    def compute_manifest_hash(manifest: Dict) -> str:
        """
        Compute SHA256 hash of manifest for integrity.

        Args:
            manifest: Curriculum manifest

        Returns:
            SHA256 hash
        """
        # Create a copy and remove the hash field if it exists to avoid circular dependency
        manifest_copy = {k: v for k, v in manifest.items() if k != "_hash"}
        
        # Convert to string with default handler for non-serializable objects
        # Don't use sort_keys as it fails with mixed None/int types
        manifest_str = json.dumps(
            manifest_copy,
            separators=(",", ":"),
            default=str,  # Convert non-serializable objects to string
        )
        return hashlib.sha256(manifest_str.encode()).hexdigest()

    @staticmethod
    def save_manifest(manifest: Dict, output_path: Path) -> None:
        """
        Save manifest to JSON file.

        Args:
            manifest: Curriculum manifest
            output_path: Path to save manifest
        """
        output_path.parent.mkdir(parents=True, exist_ok=True)

        # Add manifest hash
        manifest["_hash"] = CurriculumManifestBuilder.compute_manifest_hash(manifest)

        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2, ensure_ascii=False)

        logger.info(f"Curriculum manifest saved to {output_path}")

    @staticmethod
    def load_manifest(manifest_path: Path) -> Dict:
        """
        Load manifest from JSON file.

        Args:
            manifest_path: Path to manifest file

        Returns:
            Loaded manifest
        """
        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)

        logger.info(f"Curriculum manifest loaded from {manifest_path}")
        return manifest

    @staticmethod
    def validate_manifest(manifest: Dict) -> bool:
        """
        Validate manifest structure.

        Args:
            manifest: Curriculum manifest

        Returns:
            True if valid
        """
        required_keys = ["metadata", "curriculum_index", "curriculum_graph"]

        for key in required_keys:
            if key not in manifest:
                logger.error(f"Missing required key: {key}")
                return False

        # Verify curriculum_index has entries
        if not manifest["curriculum_index"]:
            logger.error("curriculum_index is empty")
            return False

        logger.info("Manifest validation passed")
        return True

    @staticmethod
    def print_manifest_summary(manifest: Dict) -> None:
        """Print manifest summary."""
        meta = manifest["metadata"]
        print("\n" + "=" * 60)
        print("CURRICULUM MANIFEST SUMMARY")
        print("=" * 60)
        print(f"Version: {meta['version']}")
        print(f"Created: {meta['created_at']}")
        print(f"Total Grades: {meta['total_grades']}")
        print(f"Total Subjects: {meta['total_subjects']}")
        print(f"Total Languages: {meta['total_languages']}")
        print(f"Total Chapters: {meta['total_chapters']}")
        print(f"Manifest Hash: {manifest.get('_hash', 'N/A')[:16]}...")
        print("\n" + "=" * 60)
