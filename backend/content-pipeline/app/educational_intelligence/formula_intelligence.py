from __future__ import annotations

import hashlib
import re
from collections import Counter
from typing import Any

from .concept_models import EducationalConcept


FORMULA_RE = re.compile(
    r"(?P<formula>(?:[A-Za-z][A-Za-z\s]{0,45}|[A-Za-z])\s*(?:=|<|>|≤|≥|≈|∝)\s*[A-Za-z0-9πθ°%+\-*/×÷^().,\s]{1,130})"
)
SYMBOLIC_RE = re.compile(r"(?P<formula>\b[A-Za-z]\s*[=<>≤≥≈∝]\s*[A-Za-z0-9πθ°%+\-*/×÷^().,\s]{1,80})")
RELATION_RE = re.compile(
    r"\b(?P<formula>(?:density|speed|velocity|acceleration|force|pressure|work|power|current|voltage|resistance|area|volume|perimeter|profit|loss)\s+(?:is|=|:)\s+[^.?!,;]{3,100})",
    re.I,
)
WORD_RE = re.compile(r"[A-Za-z0-9]+")
VARIABLE_RE = re.compile(r"\b([A-Za-z])\b")
EXPLANATION_SIGNALS = ("because", "therefore", "hence", "means", "relationship", "law", "principle", "depends", "for example")
REJECT_TYPES = {"metadata", "table_of_contents", "index_page", "activity", "assessment"}


class FormulaIntelligence:
    """Detect, explain, and publish formulas as first-class educational context."""

    def enhance(
        self,
        rows: list[dict[str, Any]],
        concepts: list[EducationalConcept],
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        enhanced = [{**row, "metadata": dict(row.get("metadata") or {})} for row in rows]
        detected: dict[str, dict[str, Any]] = {}
        source_formulas = self._source_formulas(concepts)

        for index, row in enumerate(enhanced):
            if self._reject(row):
                continue
            formulas = self.detect(str(row.get("text") or ""))
            if not formulas:
                continue
            metadata_formulas = []
            for formula in formulas:
                info = self._metadata_for_formula(formula, rows, index)
                detected[self._key(formula)] = info
                metadata_formulas.append(info)
            row["metadata"]["formula_intelligence"] = metadata_formulas
            row["metadata"]["formula_count"] = len(metadata_formulas)
            if str(row["metadata"].get("content_type") or "") == "concept":
                row["metadata"]["content_type"] = "formula_explanation"

        for formula in source_formulas:
            key = self._key(formula)
            if key not in detected:
                detected[key] = self._metadata_for_formula(formula, enhanced, None)
            enhanced.append(self._formula_context_row(detected[key], len(enhanced) + 1))

        explained = sum(1 for item in detected.values() if item.get("explanation"))
        variable_ready = sum(1 for item in detected.values() if item.get("variables"))
        return enhanced, {
            "source_formulas": len(source_formulas),
            "detected_formulas": len(detected),
            "coverage": self._coverage(source_formulas, detected),
            "explained_formulas": explained,
            "formula_explanation_rate": self._percent(explained, len(detected)),
            "variable_coverage": self._percent(variable_ready, len(detected)),
            "formulas": list(detected.values())[:120],
        }

    def detect(self, text: str) -> list[str]:
        formulas = []
        for pattern in (FORMULA_RE, SYMBOLIC_RE, RELATION_RE):
            for match in pattern.finditer(text or ""):
                formula = self._clean_formula(match.group("formula"))
                if formula:
                    formulas.append(formula)
        return list(dict.fromkeys(formulas))

    def _metadata_for_formula(self, formula: str, rows: list[dict[str, Any]], index: int | None) -> dict[str, Any]:
        explanation = self._recover_explanation(formula, rows, index)
        variables = self._variables(formula, explanation)
        return {
            "formula": formula,
            "formula_type": self._formula_type(formula),
            "meaning": self._meaning(formula, explanation),
            "variables": variables,
            "units": self._units(formula),
            "example": self._example(explanation),
            "explanation": explanation,
            "common_mistakes": self._common_mistakes(formula),
            "related_concepts": self._related_concepts(formula, explanation),
        }

    def _recover_explanation(self, formula: str, rows: list[dict[str, Any]], index: int | None) -> str:
        candidates = []
        if index is None:
            for row in rows:
                text = str(row.get("text") or "")
                if self._formula_overlap(formula, text):
                    candidates.append(text)
        else:
            for pos in range(max(0, index - 2), min(len(rows), index + 3)):
                if not self._reject(rows[pos]):
                    candidates.append(str(rows[pos].get("text") or ""))
        sentences = []
        for text in candidates:
            for sentence in split_sentences(text):
                lowered = sentence.lower()
                if formula in sentence or any(signal in lowered for signal in EXPLANATION_SIGNALS) or self._formula_overlap(formula, sentence):
                    sentences.append(sentence)
                if len(sentences) >= 5:
                    break
        if not sentences:
            return f"{formula} expresses a relationship between the quantities in this chapter."
        return " ".join(dict.fromkeys(sentences))[:1000]

    @staticmethod
    def _formula_context_row(info: dict[str, Any], sequence: int) -> dict[str, Any]:
        formula = info["formula"]
        text = "\n".join(
            [
                f"Formula: {formula}",
                f"Meaning: {info['meaning']}",
                f"Variables: " + "; ".join(f"{key}: {value}" for key, value in info.get("variables", {}).items()),
                f"Explanation: {info.get('explanation') or info['meaning']}",
                f"Example: {info.get('example') or 'Use the formula by identifying known values and substituting carefully.'}",
            ]
        )
        return {
            "chunk_id": f"formula_context_{sequence}_{hashlib.sha256(formula.lower().encode('utf-8')).hexdigest()[:12]}",
            "text": text,
            "metadata": {
                "content_type": "formula_explanation",
                "rag_eligible": True,
                "formula_intelligence": [info],
                "key_terms": [formula, *info.get("related_concepts", [])],
                "related_concepts": info.get("related_concepts", []),
                "quality_class": "GOOD",
            },
            "embedding": [],
        }

    @staticmethod
    def _source_formulas(concepts: list[EducationalConcept]) -> list[str]:
        formulas = []
        for concept in concepts:
            formulas.extend(concept.formulas)
        return list(dict.fromkeys(FormulaIntelligence._clean_formula(formula) for formula in formulas if FormulaIntelligence._clean_formula(formula)))

    @staticmethod
    def _clean_formula(value: str) -> str:
        value = re.sub(r"\s+", " ", str(value or "")).strip(" .,:;")
        if len(value) < 3 or len(value) > 150:
            return ""
        if not any(symbol in value for symbol in ("=", "<", ">", "≤", "≥", "≈", "∝")):
            return ""
        return value

    @staticmethod
    def _formula_type(formula: str) -> str:
        lowered = formula.lower()
        if any(term in lowered for term in ("force", "pressure", "density", "speed", "velocity", "current", "voltage", "resistance")):
            return "scientific_law"
        if any(symbol in formula for symbol in ("∝", "≤", "≥", "<", ">")):
            return "relationship"
        if "=" in formula:
            return "equation"
        return "symbolic_expression"

    @staticmethod
    def _meaning(formula: str, explanation: str) -> str:
        if explanation:
            return split_sentences(explanation)[0][:280]
        return f"{formula} describes how the quantities in the expression are related."

    @staticmethod
    def _variables(formula: str, explanation: str) -> dict[str, str]:
        variables = {}
        for variable in VARIABLE_RE.findall(formula):
            if variable.lower() in {"a", "i"}:
                continue
            variables.setdefault(variable, f"Quantity represented by {variable}")
        lowered = f"{formula} {explanation}".lower()
        known = {
            "F": "force",
            "m": "mass",
            "a": "acceleration",
            "V": "voltage or volume depending on context",
            "I": "current",
            "R": "resistance",
            "P": "pressure or power depending on context",
            "A": "area",
        }
        for symbol, meaning in known.items():
            if re.search(rf"\b{re.escape(symbol)}\b", formula) or meaning.split()[0] in lowered:
                variables[symbol] = meaning
        return variables

    @staticmethod
    def _units(formula: str) -> dict[str, str]:
        lowered = formula.lower()
        units = {}
        if "force" in lowered or "f=" in lowered.replace(" ", ""):
            units["force"] = "newton"
        if "pressure" in lowered:
            units["pressure"] = "pascal"
        if "density" in lowered:
            units["density"] = "kg/m^3 or g/cm^3"
        if "current" in lowered:
            units["current"] = "ampere"
        if "voltage" in lowered:
            units["voltage"] = "volt"
        return units

    @staticmethod
    def _example(explanation: str) -> str:
        for sentence in split_sentences(explanation):
            if any(marker in sentence.lower() for marker in ("example", "suppose", "consider", "if ")):
                return sentence[:320]
        return ""

    @staticmethod
    def _common_mistakes(formula: str) -> list[str]:
        mistakes = ["Substituting values without checking the units.", "Using the formula without identifying each variable."]
        if "/" in formula or "÷" in formula:
            mistakes.append("Reversing numerator and denominator.")
        return mistakes[:3]

    @staticmethod
    def _related_concepts(formula: str, explanation: str) -> list[str]:
        counts = Counter(
            token.lower()
            for token in WORD_RE.findall(f"{formula} {explanation}")
            if len(token) >= 4 and not token.isdigit() and token.lower() not in {"formula", "example", "chapter", "quantity", "value"}
        )
        return [term.title() for term, _count in counts.most_common(8)]

    @staticmethod
    def _reject(row: dict[str, Any]) -> bool:
        metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        return str(metadata.get("content_type") or "") in REJECT_TYPES or str(metadata.get("quality_class") or "") in {"OCR_NOISE", "HEADER_FOOTER", "EMPTY"}

    @staticmethod
    def _formula_overlap(formula: str, text: str) -> bool:
        formula_terms = {token.lower() for token in WORD_RE.findall(formula) if len(token) >= 2}
        text_terms = {token.lower() for token in WORD_RE.findall(text) if len(token) >= 2}
        return bool(formula_terms & text_terms) and any(symbol in text for symbol in ("=", "<", ">", "≤", "≥", "≈", "∝", ":"))

    @staticmethod
    def _key(formula: str) -> str:
        return re.sub(r"\s+", " ", formula.lower()).strip()

    @staticmethod
    def _coverage(source_formulas: list[str], detected: dict[str, dict[str, Any]]) -> float:
        if not source_formulas:
            return 100.0
        detected_text = " ".join(item["formula"] for item in detected.values()).lower()
        retained = sum(1 for formula in source_formulas if formula.lower() in detected_text)
        return FormulaIntelligence._percent(retained, len(source_formulas))

    @staticmethod
    def _percent(value: int, total: int) -> float:
        if not total:
            return 100.0
        return round(100.0 * value / total, 2)


def split_sentences(text: str) -> list[str]:
    return [item.strip() for item in re.split(r"(?<=[.!?])\s+", text or "") if item.strip()]
