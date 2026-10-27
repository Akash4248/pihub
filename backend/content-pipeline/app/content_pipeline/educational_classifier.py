from __future__ import annotations

import re


class EducationalClassifier:
    """Classify chunk type for educational ranking and retrieval.

    Distinguishes formulas, definitions, examples, experiments, and questions
    with high precision, avoiding false positives on hyphenated words or
    plain conversational text. Supports English and Indic NCERT phrasing.
    """

    FORMULA_PATTERNS = [
        re.compile(r"\$\$.+?\$\$", re.DOTALL),
        re.compile(r"(?:\b[a-zA-Z0-9_]+\s*=\s*[-+]?[a-zA-Z0-9_\(\)\s*/^+-]{2,})"),
        re.compile(r"\b\d+\s*[a-zA-Z0-9_]*\s*[\+*/]\s*\d+\s*[a-zA-Z0-9_]*\s*="),
        re.compile(r"\b[a-zA-Z0-9_]+\s*(?:->|→)\s*[a-zA-Z0-9_\s+]+"),  # Chemical reaction
        re.compile(r"\\(?:frac|sqrt|sum|int|times|div|approx|neq|le|ge|alpha|beta|theta|pi)\b"),
        re.compile(r"\b(?:formula|equation)\s*[:\-]\s*\S+", re.IGNORECASE),
    ]

    DEFINITION_PATTERNS = [
        re.compile(r"^(?:#+\s*)?(?:definition|ವ್ಯಾಖ್ಯಾನ|परिभाषा)\s*[:\-]", re.IGNORECASE | re.MULTILINE),
        re.compile(r"\b(?:is|are)\s+(?:called|known as|defined as|termed as|referred to as)\b", re.IGNORECASE),
        re.compile(r"\b(?:refers to|means|may be defined as)\b", re.IGNORECASE),
        re.compile(r"^(?:#+\s*)?what (?:is|are)\s+[\w\s]{2,40}\??$", re.IGNORECASE | re.MULTILINE),
        re.compile(r"\b(?:ಎಂದು ಕರೆಯಲಾಗುತ್ತದೆ|ಎನ್ನಲಾಗುತ್ತದೆ|कहते हैं|कहा जाता है)\b"),
    ]

    EXAMPLE_PATTERNS = [
        re.compile(r"^(?:#+\s*)?(?:example|worked example|illustration|ಉದಾಹರಣೆ|उदाहरण)\s*[\d.:\-]", re.IGNORECASE | re.MULTILINE),
        re.compile(r"\b(?:for example|for instance|e\.g\.)\b", re.IGNORECASE),
    ]

    EXPERIMENT_PATTERNS = [
        re.compile(r"^(?:#+\s*)?(?:activity|experiment|investigation|procedure|observation|ಚಟುವಟಿಕೆ|ಪ್ರಯೋಗ|क्रियाकलाप)\s*[\d.:\-]", re.IGNORECASE | re.MULTILINE),
        re.compile(r"\b(?:materials required|let us perform an activity|step-by-step procedure)\b", re.IGNORECASE),
    ]

    QA_PATTERNS = [
        re.compile(r"^(?:#+\s*)?(?:q\.|question|exercise|problem|check your progress|ಅಭ್ಯಾಸ|ಪ್ರಶ್ನೆ|अभ्यास|प्रश्न)\s*[\d.:\-]", re.IGNORECASE | re.MULTILINE),
        re.compile(r"^(?:#+\s*)?(?:ans\.|answer|solution|ಪರಿಹಾರ|उत्तर)\s*[\d.:\-]", re.IGNORECASE | re.MULTILINE),
    ]

    def classify(self, text: str) -> str:
        trimmed = text.strip()
        if not trimmed:
            return "explanation"

        # Check explicit structural prefixes first (highest confidence)
        if any(p.search(trimmed) for p in self.QA_PATTERNS):
            return "qa"
        if any(p.search(trimmed) for p in self.EXPERIMENT_PATTERNS):
            return "experiment"
        if any(p.search(trimmed) for p in self.EXAMPLE_PATTERNS):
            return "example"
        if any(p.search(trimmed) for p in self.DEFINITION_PATTERNS):
            return "definition"

        # Check for formula blocks
        if any(p.search(trimmed) for p in self.FORMULA_PATTERNS):
            # Verify it is not merely mentioning the word 'formula' in a sentence
            lines = [line.strip() for line in trimmed.splitlines() if line.strip()]
            math_lines = sum(1 for line in lines if any(p.search(line) for p in self.FORMULA_PATTERNS))
            if math_lines >= 1 and (math_lines / max(len(lines), 1) >= 0.25 or len(trimmed) < 250):
                return "formula"

        return "explanation"
