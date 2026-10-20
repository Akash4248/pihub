"""Subject mapping from directory names and curriculum hierarchies."""

import re
from pathlib import Path
from typing import Optional


class SubjectMapper:
    """Map directory names to curriculum subjects."""

    # Subject patterns
    SUBJECT_PATTERNS = {
        "mathematics": [r"maths?", r"math\s", r"numeracy"],
        "science": [r"science(?!\s)", r"sciences\s", r"science\s"],
        "chemistry": [r"chemistry"],
        "physics": [r"physics"],
        "biology": [r"biology"],
        "social_science": [r"social\s+science", r"social science", r"ss\s+"],
        "english": [r"english\s", r"language\s+english"],
        "hindi": [r"hindi\s", r"language\s+hindi"],
        "social_studies": [r"social\s+studies", r"studies"],
        "computer_science": [r"computer", r"computing", r"ict"],
    }

    SUBJECT_ALIASES = {
        "math": "mathematics",
        "maths": "mathematics",
        "chem": "chemistry",
        "phy": "physics",
        "bio": "biology",
        "social": "social_science",
        "ss": "social_science",
        "computers": "computer_science",
        "it": "computer_science",
    }

    @classmethod
    def extract_subject_from_directory(cls, dir_path: Path) -> Optional[str]:
        """
        Extract subject from directory name.

        Examples:
            "maths 1 to 10" -> "mathematics"
            "science 5-10" -> "science"
            "social science 6-10" -> "social_science"

        Args:
            dir_path: Directory path

        Returns:
            Subject code or None
        """
        dir_name = dir_path.name.lower()

        # Direct pattern matching
        for subject, patterns in cls.SUBJECT_PATTERNS.items():
            for pattern in patterns:
                if re.search(pattern, dir_name, re.IGNORECASE):
                    return subject

        # Check for aliases as standalone tokens, not substrings ("ss" in "class").
        for alias, normalized in cls.SUBJECT_ALIASES.items():
            if re.search(rf"(?:^|[\s_-]){re.escape(alias)}(?:$|[\s_-])", dir_name, re.IGNORECASE):
                return normalized

        return None

    @classmethod
    def extract_subject_range(cls, dir_name: str) -> Optional[tuple]:
        """
        Extract grade range from subject directory.

        Examples:
            "maths 1 to 10" -> (1, 10)
            "science 5-10" -> (5, 10)

        Args:
            dir_name: Directory name

        Returns:
            Tuple of (start_grade, end_grade) or None
        """
        # Pattern: "subject start to end" or "subject start-end"
        match = re.search(r"(\d+)\s*(?:to|-)\s*(\d+)", dir_name, re.IGNORECASE)
        if match:
            return (int(match.group(1)), int(match.group(2)))

        return None

    @classmethod
    def normalize_subject(cls, raw_subject: str) -> str:
        """
        Normalize subject name.

        Args:
            raw_subject: Raw subject string

        Returns:
            Normalized subject code
        """
        normalized = raw_subject.lower().strip()

        if normalized in cls.SUBJECT_ALIASES:
            return cls.SUBJECT_ALIASES[normalized]

        return normalized
