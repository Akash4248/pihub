"""
NCERT QR Code & Dial Code Mapper
Maps NCERT Energized Textbook QR Codes (e.g., 11062CH01) to Chapter metadata,
learning outcomes, and DIKSHA interactive objects.
"""

import re
import logging
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)

# Standard NCERT Subject Code Mappings
NCERT_SUBJECT_CODES = {
    "062": {"subject": "Physics", "grade": 11},
    "063": {"subject": "Chemistry", "grade": 11},
    "064": {"subject": "Mathematics", "grade": 11},
    "065": {"subject": "Biology", "grade": 11},
    "122": {"subject": "Physics", "grade": 12},
    "123": {"subject": "Chemistry", "grade": 12},
    "124": {"subject": "Mathematics", "grade": 12},
    "125": {"subject": "Biology", "grade": 12},
}

class NCERTQRMapper:
    """
    Decodes and maps NCERT QR Dial Codes to curriculum nodes and DIKSHA assets.
    """

    def parse_qr_code(self, qr_code_str: str) -> Dict[str, Any]:
        """
        Parses standard NCERT QR code strings.
        Example: 11062CH01 -> Grade 11, Subject 062 (Physics), Chapter 1.
        """
        code = qr_code_str.strip().upper()

        # Match pattern: {Grade 2 digits}{Subject 3 digits}CH{Chapter 2 digits}
        match = re.match(r"^(\d{2})(\d{3})CH(\d{1,2})$", code)
        if match:
            grade_num = int(match.group(1))
            subject_code = match.group(2)
            chapter_num = int(match.group(3))

            subject_info = NCERT_SUBJECT_CODES.get(
                subject_code, {"subject": "General Science", "grade": grade_num}
            )

            return {
                "valid": True,
                "qr_code": code,
                "grade": f"Class {grade_num}",
                "grade_num": grade_num,
                "subject": subject_info["subject"],
                "subject_code": subject_code,
                "chapter_num": chapter_num,
                "chapter_id": f"class_{grade_num}_{subject_info['subject'].lower()}_ch{chapter_num}",
            }

        # Fallback parsing for general codes
        return {
            "valid": False,
            "qr_code": code,
            "grade": "Class 11",
            "grade_num": 11,
            "subject": "General Science",
            "subject_code": "000",
            "chapter_num": 1,
            "chapter_id": "class_11_science_ch1",
        }

    def map_qr_to_chapter(self, qr_code_str: str) -> Dict[str, Any]:
        """
        Maps parsed QR code to comprehensive chapter learning outcomes and practice links.
        """
        parsed = self.parse_qr_code(qr_code_str)

        grade = parsed["grade"]
        subject = parsed["subject"]
        chap_num = parsed["chapter_num"]

        return {
            "qr_code": parsed["qr_code"],
            "chapter_id": parsed["chapter_id"],
            "grade": grade,
            "subject": subject,
            "chapter_number": chap_num,
            "chapter_title": f"{subject} Chapter {chap_num} Core Concepts",
            "learning_outcomes": [
                f"Master core theorems and formulations in {subject} Chapter {chap_num}.",
                "Develop conceptual clarity and quantitative problem-solving skills.",
                "Perform interactive simulations to visualize key physical/math behaviors."
            ],
            "diksha_resource_id": f"do_diksha_{subject.lower()}_ch{chap_num}",
            "diksha_url": f"https://diksha.gov.in/dial/{parsed['qr_code']}",
        }
