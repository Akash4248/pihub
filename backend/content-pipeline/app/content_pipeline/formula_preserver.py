from __future__ import annotations

import re


class FormulaPreserver:
    """Detect and preserve formulas/equations as atomic blocks."""

    FORMULA_LINE = re.compile(
        r"(\$\$.+?\$\$|"
        r"\b[a-zA-Z0-9_]+\s*=\s*[-+]?[a-zA-Z0-9_\(\)\s*/^+-]{2,}|"
        r"\b\d+\s*[a-zA-Z0-9_]*\s*[\+*/]\s*\d+\s*[a-zA-Z0-9_]*\s*=|"
        r"\b[a-zA-Z0-9_]+\s*(?:->|→)\s*[a-zA-Z0-9_\s+]+|"
        r"\bE\s*=\s*mc\^?2\b|"
        r"\\[a-zA-Z]+|"
        r"[=∝≈≠≤≥∆π√∠])"
    )

    def is_formula_block(self, paragraph: str) -> bool:
        lines = [line.strip() for line in paragraph.splitlines() if line.strip()]
        if not lines:
            return False
        # If the block has display math, it is always a formula block
        if "$$" in paragraph:
            return True
        matches = sum(1 for line in lines if self.FORMULA_LINE.search(line))
        return matches >= max(1, (len(lines) + 1) // 2)
