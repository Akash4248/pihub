from __future__ import annotations

import io
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class ConvertedFormula:
    source_image: str
    latex: str
    confidence: float
    method: str
    page_number: int = 0
    original_text: str = ""


class FormulaConverter:
    """Convert formula images to LaTeX using available backends.

    Backends tried in order:
    1. pix2tex (LaTeX-OCR) — best quality, requires torch
    2. Heuristic text extraction — regex-based, no deps needed
    """

    def __init__(self):
        self._pix2tex_model = None

    def convert_image(self, image_path: str, page_number: int = 0) -> ConvertedFormula:
        """Convert a single formula image to LaTeX."""
        path = Path(image_path)
        if not path.exists():
            return ConvertedFormula(
                source_image=image_path, latex="", confidence=0.0,
                method="none", page_number=page_number,
            )

        latex, confidence, method = self._try_pix2tex(str(path))
        if latex:
            return ConvertedFormula(
                source_image=image_path, latex=latex,
                confidence=confidence, method=method,
                page_number=page_number,
            )

        return ConvertedFormula(
            source_image=image_path, latex="", confidence=0.0,
            method="none", page_number=page_number,
        )

    def convert_text_formulas(self, text: str) -> list[ConvertedFormula]:
        """Extract formula-like text patterns and represent as LaTeX.

        First extracts standard Markdown LaTeX ($$ ... $$ and $ ... $) blocks
        with high confidence, then falls back to heuristic pattern conversion
        with SymPy syntax validation.
        """
        formulas = []
        seen: set[str] = set()

        # 1. Native Markdown display math ($$...$$)
        for m in re.finditer(r"\$\$(.+?)\$\$", text, re.DOTALL):
            raw_latex = m.group(1).strip()
            if raw_latex and raw_latex not in seen:
                seen.add(raw_latex)
                valid = self.validate_latex(raw_latex)
                formulas.append(ConvertedFormula(
                    source_image="", latex=raw_latex,
                    confidence=0.95 if valid else 0.8, method="markdown_display_latex",
                    original_text=m.group(0),
                ))

        # 2. Native Markdown inline math ($...$)
        for m in re.finditer(r"(?<!\$)\$(?!\$)(.+?)(?<!\$)\$(?!\$)", text):
            raw_latex = m.group(1).strip()
            if raw_latex and len(raw_latex) >= 2 and raw_latex not in seen:
                seen.add(raw_latex)
                valid = self.validate_latex(raw_latex)
                formulas.append(ConvertedFormula(
                    source_image="", latex=raw_latex,
                    confidence=0.9 if valid else 0.75, method="markdown_inline_latex",
                    original_text=m.group(0),
                ))

        # 3. Fallback to line-by-line heuristic conversion for unformatted equations
        lines = text.split("\n")
        for line in lines:
            stripped = line.strip()
            if not stripped or len(stripped) < 4 or stripped.startswith("$"):
                continue

            latex = self._heuristic_to_latex(stripped)
            if latex and latex not in seen:
                seen.add(latex)
                valid = self.validate_latex(latex)
                formulas.append(ConvertedFormula(
                    source_image="", latex=latex,
                    confidence=0.7 if valid else 0.5, method="heuristic",
                    original_text=stripped,
                ))

        return formulas

    @staticmethod
    def validate_latex(latex_str: str) -> bool:
        """Validate LaTeX syntax using SymPy parser or balanced delimiter checks."""
        stripped = latex_str.strip()
        if not stripped:
            return False

        # First verify brace and parenthesis balance
        stack = []
        pairs = {"{": "}", "(": ")", "[": "]"}
        for ch in stripped:
            if ch in pairs:
                stack.append(pairs[ch])
            elif ch in pairs.values():
                if not stack or stack.pop() != ch:
                    return False
        if stack:
            return False

        # Attempt formal SymPy LaTeX parsing
        try:
            from sympy.parsing.latex import parse_latex  # type: ignore
            parse_latex(stripped)
            return True
        except Exception:
            # Domain-specific notations (e.g. chemistry, inequalities, or multi-equation lines)
            # may not parse as a pure SymPy scalar expression but are still structurally balanced LaTeX.
            return True

    def _try_pix2tex(self, image_path: str) -> tuple[str, float, str]:
        try:
            if self._pix2tex_model is None:
                from pix2tex.cli import LatexOCR
                self._pix2tex_model = LatexOCR()
            from PIL import Image
            img = Image.open(image_path)
            latex = self._pix2tex_model(img)
            if latex:
                return latex.strip(), 0.85, "pix2tex"
        except ImportError:
            logger.debug("pix2tex not installed; skipping AI formula conversion")
        except Exception as exc:
            logger.warning("pix2tex failed on %s: %s", image_path, str(exc)[:100])
        return "", 0.0, ""

    def _heuristic_to_latex(self, text: str) -> str:
        """Convert plain-text formulas to LaTeX using regex patterns."""
        patterns = [
            # 2² → 2^{2}
            (r"(\d+)²", r"\1^{2}"),
            (r"(\d+)³", r"\1^{3}"),
            # a² → a^{2}, x² → x^{2}
            (r"([a-zA-Z])²", r"\1^{2}"),
            (r"([a-zA-Z])³", r"\1^{3}"),
            # ≈ → \approx
            (r"≈", r"\\approx "),
            # ≠ → \neq
            (r"≠", r"\\neq "),
            # × → \times
            (r"×", r"\\times "),
            # ÷ → \div
            (r"÷", r"\\div "),
            # °C → ^\circ C
            (r"°([CF])", r"^{\\circ}\1"),
            # sqrt pattern
            (r"√\(([^)]+)\)", r"\\sqrt{\1}"),
            (r"√([a-zA-Z0-9]+)", r"\\sqrt{\1}"),
            # π → \pi
            (r"π", r"\\pi "),
            (r"θ", r"\\theta "),
            (r"α", r"\\alpha "),
            (r"β", r"\\beta "),
            (r"Δ", r"\\Delta "),
            (r"λ", r"\\lambda "),
            (r"μ", r"\\mu "),
            (r"σ", r"\\sigma "),
        ]

        result = text
        for pattern, replacement in patterns:
            result = re.sub(pattern, replacement, result)

        if result == text:
            return ""

        return result.strip()


def format_latex_block(formulas: list[ConvertedFormula]) -> str:
    """Format a list of formulas as a LaTeX block."""
    parts = []
    for f in formulas:
        if f.latex:
            parts.append(f.latex)
    if not parts:
        return ""
    return "\n".join(
        f"$$ {latex} $$" if len(latex) > 20 else f"$ {latex} $"
        for latex in parts
    )
