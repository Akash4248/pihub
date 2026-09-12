from __future__ import annotations

import re
from dataclasses import dataclass


SEMVER_PATTERN = re.compile(r"^(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:[-+].*)?$")


@dataclass(frozen=True)
class SemVer:
    major: int
    minor: int
    patch: int

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"


class VersionManager:
    @staticmethod
    def parse(version: str) -> SemVer:
        match = SEMVER_PATTERN.match(version.strip())
        if not match:
            raise ValueError(f"Invalid semantic version: {version}")
        major = int(match.group(1))
        minor = int(match.group(2)) if match.group(2) is not None else 0
        patch = int(match.group(3)) if match.group(3) is not None else 0
        return SemVer(major, minor, patch)

    @classmethod
    def normalize(cls, version: str) -> str:
        return str(cls.parse(version))

    @classmethod
    def compare(cls, left: str, right: str) -> int:
        left_semver = cls.parse(left)
        right_semver = cls.parse(right)
        left_tuple = (left_semver.major, left_semver.minor, left_semver.patch)
        right_tuple = (right_semver.major, right_semver.minor, right_semver.patch)
        if left_tuple < right_tuple:
            return -1
        if left_tuple > right_tuple:
            return 1
        return 0

    @classmethod
    def bump_patch(cls, version: str) -> str:
        parsed = cls.parse(version)
        return str(SemVer(parsed.major, parsed.minor, parsed.patch + 1))

    @classmethod
    def bump_minor(cls, version: str) -> str:
        parsed = cls.parse(version)
        return str(SemVer(parsed.major, parsed.minor + 1, 0))

    @classmethod
    def bump_major(cls, version: str) -> str:
        parsed = cls.parse(version)
        return str(SemVer(parsed.major + 1, 0, 0))
