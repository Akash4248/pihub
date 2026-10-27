from __future__ import annotations

import re


class ConceptBoundaryDetector:
    """Detect boundaries around definition/example/experiment/QA blocks."""

    DEFINITION_PATTERNS = [
        re.compile(r"^(?:#+\s*)?(?:definition|ವ್ಯಾಖ್ಯಾನ|परिभाषा)\s*[:\-]", re.IGNORECASE),
        re.compile(r"^(?:#+\s*)?what (?:is|are)\s+", re.IGNORECASE),
        re.compile(r"\b(?:is|are)\s+(?:called|known as|defined as|termed as)\b", re.IGNORECASE),
        re.compile(r"\b(?:refers to|means|may be defined as)\b", re.IGNORECASE),
    ]
    EXAMPLE_PATTERNS = [
        re.compile(r"^(?:#+\s*)?(?:example|worked example|illustration|ಉದಾಹರಣೆ|उदाहरण)\s*[\d.:\-]", re.IGNORECASE),
        re.compile(r"^(?:#+\s*)?for example\b", re.IGNORECASE),
    ]
    EXPERIMENT_PATTERNS = [
        re.compile(r"^(?:#+\s*)?(?:experiment|activity|investigation|procedure|observation|ಚಟುವಟಿಕೆ|ಪ್ರಯೋಗ|क्रियाकलाप)\s*[\d.:\-]", re.IGNORECASE),
        re.compile(r"\blet us perform an activity\b", re.IGNORECASE),
    ]
    QA_PATTERNS = [
        re.compile(r"^(?:#+\s*)?(?:q\.|question|exercise|problem|ಅಭ್ಯಾಸ|ಪ್ರಶ್ನೆ|अभ्यास|प्रश्न)\s*[\d.:\-]", re.IGNORECASE),
        re.compile(r"^(?:#+\s*)?(?:a\.|ans\.|answer|solution|ಪರಿಹಾರ|उत्तर)\s*[\d.:\-]", re.IGNORECASE),
    ]

    def is_boundary_start(self, paragraph: str) -> bool:
        text = paragraph.strip()
        if not text:
            return False
        return any(
            pattern.search(text)
            for pattern in [
                *self.DEFINITION_PATTERNS,
                *self.EXAMPLE_PATTERNS,
                *self.EXPERIMENT_PATTERNS,
                *self.QA_PATTERNS,
            ]
        )

    def boundary_label(self, paragraph: str) -> str | None:
        text = paragraph.strip()
        if any(p.search(text) for p in self.DEFINITION_PATTERNS):
            return "definition"
        if any(p.search(text) for p in self.EXPERIMENT_PATTERNS):
            return "experiment"
        if any(p.search(text) for p in self.EXAMPLE_PATTERNS):
            return "example"
        if any(p.search(text) for p in self.QA_PATTERNS):
            return "qa"
        return None
