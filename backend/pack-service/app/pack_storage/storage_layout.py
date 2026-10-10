from __future__ import annotations

import re

from shared.text_normalization import normalize_curriculum_name


SLUG_PATTERN = re.compile(r"[^a-z0-9]+")


class StorageLayout:
    @staticmethod
    def slugify(value: str) -> str:
        # Same normalization as pack-id parts (PackGenerator._pack_id_part)
        # so directory names and pack ids can never diverge on one input.
        # NOTE: already-canonical pack_ids must bypass normalize (it strips
        # trailing numbers) — see pack_directory_name below.
        normalized = normalize_curriculum_name(value)
        slug = SLUG_PATTERN.sub("_", normalized).strip("_")
        return slug or "unknown"

    @classmethod
    def pack_directory_name(cls, grade: int | None, subject: str | None, chapter: str | None, pack_id: str) -> str:
        grade_segment = f"grade_{grade}" if grade is not None else "grade_unknown"
        subject_segment = cls.slugify(subject or "unknown")
        chapter_segment = cls.slugify(chapter or "general")
        # pack_id is already a canonical slug (e.g. class_7_science_en):
        # slug it raw so stored directory names never shift under upgrades.
        pack_segment = SLUG_PATTERN.sub("_", pack_id.lower()).strip("_") or "unknown"
        return f"{grade_segment}/{subject_segment}/{chapter_segment}/{pack_segment}"
