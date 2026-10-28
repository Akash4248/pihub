from __future__ import annotations

import hashlib
import re
from collections import Counter, defaultdict
from typing import Any

from .concept_models import EducationalConcept


WORD_RE = re.compile(r"[A-Za-z0-9]+")
REJECT_TYPES = {"activity", "assessment", "exercise", "metadata", "table_of_contents", "index_page"}
STOP_TERMS = {
    "about",
    "activity",
    "chapter",
    "class",
    "example",
    "exercise",
    "figure",
    "from",
    "have",
    "images",
    "lesson",
    "question",
    "table",
    "that",
    "their",
    "there",
    "these",
    "this",
    "those",
    "with",
}


class TutorContextBuilder:
    """Add teaching context that helps the tutor explain, connect, and correct concepts."""

    def build(
        self,
        rows: list[dict[str, Any]],
        concepts: list[EducationalConcept],
        concept_graph: dict[str, Any] | None = None,
    ) -> tuple[list[dict[str, Any]], list[EducationalConcept], dict[str, Any]]:
        graph_edges = self._graph_edges(concept_graph or {})
        enhanced_rows = [{**row, "metadata": dict(row.get("metadata") or {})} for row in rows]
        context_rows: list[dict[str, Any]] = []
        packages: list[dict[str, Any]] = []
        misconception_rows: list[dict[str, Any]] = []
        relationship_edges: set[tuple[str, str]] = set()

        for concept in concepts[:80]:
            related_rows = self._related_rows(concept, enhanced_rows)
            package = self._package_for_concept(concept, related_rows, graph_edges)
            packages.append(package)
            misconception_rows.extend(
                {"concept": concept.name, "misconception": item, "source": "tutor_context_builder"}
                for item in package["common_misconceptions"]
            )
            relationship_edges.update((concept.name, item) for item in package["related_concepts"] if item != concept.name)
            self._apply_package(concept, package)
            self._attach_to_source_rows(enhanced_rows, related_rows, package)
            context_rows.append(self._context_row(package, len(context_rows) + 1))

        enhanced_rows.extend(context_rows)
        report = {
            "concepts_examined": len(concepts[:80]),
            "concepts_enriched": len(packages),
            "tutor_context_rows_created": len(context_rows),
            "prerequisite_coverage_percent": percent(sum(1 for item in packages if item["prerequisites"]), len(packages)),
            "related_concept_coverage_percent": percent(sum(1 for item in packages if item["related_concepts"]), len(packages)),
            "misconception_coverage_percent": percent(sum(1 for item in packages if item["common_misconceptions"]), len(packages)),
            "why_it_matters_coverage_percent": percent(sum(1 for item in packages if item["why_it_matters"]), len(packages)),
            "real_world_application_coverage_percent": percent(sum(1 for item in packages if item["real_world_applications"]), len(packages)),
            "formula_context_count": sum(1 for item in packages if item["formula_context"]),
            "concept_relationship_graph": {
                "nodes": sorted({edge[0] for edge in relationship_edges} | {edge[1] for edge in relationship_edges})[:200],
                "edges": [{"source": source, "target": target} for source, target in sorted(relationship_edges)[:400]],
            },
            "misconceptions": misconception_rows[:400],
            "sample_context": packages[:20],
        }
        return enhanced_rows, concepts, report

    def _package_for_concept(
        self,
        concept: EducationalConcept,
        rows: list[dict[str, Any]],
        graph_edges: dict[str, list[str]],
    ) -> dict[str, Any]:
        text = " ".join(str(row.get("text") or "") for row in rows)
        metadata = first_metadata(rows)
        subject = str(metadata.get("subject") or concept.metadata.get("subject") or "").lower()
        chapter = str(metadata.get("chapter") or concept.metadata.get("chapter") or "").lower()
        key_terms = top_terms(" ".join([concept.name, concept.definition, text]), limit=16)
        prerequisites = unique(
            [
                *concept.prerequisites,
                *self._subject_prerequisites(subject, chapter, concept.name, key_terms),
                *self._context_prerequisites(text),
            ]
        )[:8]
        related = unique(
            [
                *concept.related_concepts,
                *graph_edges.get(concept.name.lower(), []),
                *[term.title() for term in key_terms if term.lower() != concept.name.lower()],
            ]
        )[:10]
        misconceptions = unique([*concept.common_misconceptions, *self._misconceptions(subject, chapter, concept.name, key_terms, text)])[:6]
        applications = unique(self._applications(subject, chapter, concept.name, key_terms))[:8]
        formula_context = self._formula_context(rows, concept)
        why = self._why_it_matters(subject, chapter, concept.name, related, applications)
        explanation = concept.definition or first_explanatory_sentence(text) or f"{concept.name} is an important idea in this chapter."
        example = first_example(text) or (concept.examples[0] if concept.examples else concept.worked_examples[0] if concept.worked_examples else "")
        objectives = unique(
            [
                *concept.learning_objectives,
                f"Explain {concept.name} in your own words.",
                f"Connect {concept.name} with {related[0] if related else 'related ideas'}." if related else "",
                f"Use {concept.name} to reason about examples from the chapter.",
            ]
        )[:6]
        return {
            "concept": concept.name,
            "definition": concept.definition,
            "explanation": explanation[:900],
            "example": example[:700],
            "worked_examples": concept.worked_examples[:3],
            "prerequisites": prerequisites,
            "related_concepts": related,
            "common_misconceptions": misconceptions,
            "why_it_matters": why,
            "real_world_applications": applications,
            "formula_context": formula_context,
            "learning_objectives": objectives,
        }

    @staticmethod
    def _apply_package(concept: EducationalConcept, package: dict[str, Any]) -> None:
        concept.prerequisites = unique([*concept.prerequisites, *package["prerequisites"]])[:10]
        concept.related_concepts = unique([*concept.related_concepts, *package["related_concepts"]])[:12]
        concept.common_misconceptions = unique([*concept.common_misconceptions, *package["common_misconceptions"]])[:8]
        concept.learning_objectives = unique([*concept.learning_objectives, *package["learning_objectives"]])[:8]
        concept.metadata["tutor_context_package"] = package

    @staticmethod
    def _attach_to_source_rows(rows: list[dict[str, Any]], related_rows: list[dict[str, Any]], package: dict[str, Any]) -> None:
        related_ids = {row.get("chunk_id") for row in related_rows}
        for row in rows:
            if row.get("chunk_id") not in related_ids:
                continue
            metadata = row["metadata"]
            metadata["tutor_context_package"] = package
            metadata["why_it_matters"] = package["why_it_matters"]
            metadata["real_world_applications"] = package["real_world_applications"]
            metadata["prerequisites"] = package["prerequisites"]
            metadata["related_concepts"] = package["related_concepts"]
            metadata["common_misconceptions"] = package["common_misconceptions"]
            metadata["learning_objective"] = package["learning_objectives"][0] if package["learning_objectives"] else metadata.get("learning_objective")
            if package["formula_context"]:
                metadata["formula_context"] = package["formula_context"]

    @staticmethod
    def _context_row(package: dict[str, Any], sequence: int) -> dict[str, Any]:
        concept = package["concept"]
        pieces = [
            f"Concept: {concept}.",
            f"Definition: {package['definition']}" if package["definition"] else "",
            f"Explanation: {package['explanation']}",
            f"Why it matters: {package['why_it_matters']}",
            "Prerequisites: " + "; ".join(package["prerequisites"]) if package["prerequisites"] else "",
            "Related concepts: " + "; ".join(package["related_concepts"]) if package["related_concepts"] else "",
            "Common misconceptions: " + "; ".join(package["common_misconceptions"]) if package["common_misconceptions"] else "",
            "Real world applications: " + "; ".join(package["real_world_applications"]) if package["real_world_applications"] else "",
            f"Example: {package['example']}" if package["example"] else "",
        ]
        if package["formula_context"]:
            formulas = [
                f"{item.get('formula')}: {item.get('meaning') or item.get('explanation')}"
                for item in package["formula_context"][:3]
                if isinstance(item, dict)
            ]
            if formulas:
                pieces.append("Formula context: " + "; ".join(formulas))
        text = " ".join(piece for piece in pieces if piece)
        digest = hashlib.sha256(f"{concept}:{sequence}".lower().encode("utf-8")).hexdigest()[:12]
        return {
            "chunk_id": f"tutor_context_{sequence}_{digest}",
            "text": text,
            "metadata": {
                "content_type": "tutor_context",
                "rag_eligible": True,
                "quality_class": "GOOD",
                "concept_name": concept,
                "key_terms": unique([concept, *package["related_concepts"]])[:12],
                "learning_objective": package["learning_objectives"][0] if package["learning_objectives"] else f"Explain {concept}.",
                "prerequisites": package["prerequisites"],
                "related_concepts": package["related_concepts"],
                "common_misconceptions": package["common_misconceptions"],
                "why_it_matters": package["why_it_matters"],
                "real_world_applications": package["real_world_applications"],
                "formula_context": package["formula_context"],
                "tutor_context_package": package,
            },
            "embedding": [],
        }

    @staticmethod
    def _related_rows(concept: EducationalConcept, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        terms = {term.lower() for term in [concept.name, *concept.related_concepts, *top_terms(concept.definition, limit=8)] if term}
        scored: list[tuple[int, dict[str, Any]]] = []
        for row in rows:
            metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
            if metadata.get("content_type") in REJECT_TYPES:
                continue
            text = str(row.get("text") or "")
            row_terms = set(top_terms(text, limit=40))
            score = sum(1 for term in terms if term in text.lower() or term in row_terms)
            if metadata.get("formula_intelligence") and concept.formulas:
                score += 2
            if score:
                scored.append((score, row))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [row for _score, row in scored[:6]]

    @staticmethod
    def _graph_edges(graph: dict[str, Any]) -> dict[str, list[str]]:
        edges: dict[str, list[str]] = defaultdict(list)
        for edge in graph.get("edges") or []:
            if not isinstance(edge, dict):
                continue
            source = str(edge.get("source") or edge.get("from") or "").strip()
            target = str(edge.get("target") or edge.get("to") or "").strip()
            if source and target:
                edges[source.lower()].append(target)
                edges[target.lower()].append(source)
        return edges

    @staticmethod
    def _subject_prerequisites(subject: str, chapter: str, concept: str, key_terms: list[str]) -> list[str]:
        lowered = " ".join([subject, chapter, concept.lower(), " ".join(key_terms)])
        prerequisites: list[str] = []
        if any(term in lowered for term in ("math", "ratio", "proportion", "number", "algebra", "equation")):
            prerequisites.extend(["number sense", "basic operations", "fractions and ratios"])
        if any(term in lowered for term in ("force", "pressure", "motion", "light", "electric", "science")):
            prerequisites.extend(["observation", "measurement", "units"])
        if any(term in lowered for term in ("plant", "photosynthesis", "biology", "health")):
            prerequisites.extend(["living things", "plant parts", "basic needs of organisms"])
        if any(term in lowered for term in ("democracy", "constitution", "government", "history", "geography", "social")):
            prerequisites.extend(["community life", "maps or timelines", "basic civic ideas"])
        return prerequisites

    @staticmethod
    def _context_prerequisites(text: str) -> list[str]:
        lowered = text.lower()
        matches = []
        for pattern in (
            r"before learning [^.]{3,80}",
            r"you should know [^.]{3,80}",
            r"depends on [^.]{3,80}",
            r"based on [^.]{3,80}",
        ):
            matches.extend(re.findall(pattern, lowered))
        return [cleanup_phrase(item) for item in matches[:4] if cleanup_phrase(item)]

    @staticmethod
    def _misconceptions(subject: str, chapter: str, concept: str, key_terms: list[str], text: str) -> list[str]:
        lowered = " ".join([subject, chapter, concept.lower(), " ".join(key_terms), text.lower()[:2000]])
        items = []
        if any(term in lowered for term in ("proportion", "ratio")):
            items.append("Equal differences are sometimes mistaken for equal ratios.")
        if any(term in lowered for term in ("fraction", "decimal")):
            items.append("A larger denominator is sometimes mistaken as always meaning a larger value.")
        if any(term in lowered for term in ("force", "motion")):
            items.append("Motion is sometimes mistaken as needing a continuous force.")
        if any(term in lowered for term in ("pressure", "area")):
            items.append("Pressure is sometimes treated as force alone, ignoring area.")
        if any(term in lowered for term in ("light", "reflection", "lens")):
            items.append("Images are sometimes treated as physical objects inside mirrors or lenses.")
        if any(term in lowered for term in ("electric", "current", "voltage", "resistance")):
            items.append("Electric current is sometimes thought to be used up by components.")
        if any(term in lowered for term in ("plant", "photosynthesis")):
            items.append("Plants are sometimes thought to get all food directly from soil.")
        if any(term in lowered for term in ("democracy", "election", "constitution")):
            items.append("Democracy is sometimes reduced to voting only, ignoring rights and institutions.")
        if any(term in lowered for term in ("resource", "environment")):
            items.append("Natural resources are sometimes assumed to be unlimited.")
        if not items:
            items.append(f"{concept} is sometimes memorised as a term without understanding when and why it is used.")
        return items

    @staticmethod
    def _applications(subject: str, chapter: str, concept: str, key_terms: list[str]) -> list[str]:
        lowered = " ".join([subject, chapter, concept.lower(), " ".join(key_terms)])
        applications = []
        if any(term in lowered for term in ("ratio", "proportion", "percentage", "number")):
            applications.extend(["shopping and budgeting", "recipes and scaling", "maps and models", "data comparison"])
        if any(term in lowered for term in ("force", "pressure", "motion", "density")):
            applications.extend(["transportation", "sports", "machine design", "safety equipment"])
        if any(term in lowered for term in ("light", "lens", "reflection")):
            applications.extend(["eyeglasses", "cameras", "mirrors", "optical instruments"])
        if any(term in lowered for term in ("electric", "current", "voltage", "resistance")):
            applications.extend(["household circuits", "electronics", "battery-powered devices"])
        if any(term in lowered for term in ("plant", "photosynthesis", "food", "health")):
            applications.extend(["agriculture", "nutrition", "ecosystems", "public health"])
        if any(term in lowered for term in ("democracy", "constitution", "government", "rights")):
            applications.extend(["elections", "citizenship", "public decisions", "rights protection"])
        if any(term in lowered for term in ("geography", "resource", "environment", "earth")):
            applications.extend(["resource planning", "weather awareness", "sustainable living", "map reading"])
        if not applications:
            applications.extend(["daily problem solving", "classroom discussion", "explaining real situations"])
        return applications

    @staticmethod
    def _why_it_matters(subject: str, chapter: str, concept: str, related: list[str], applications: list[str]) -> str:
        related_text = f" It also connects with {related[0]}." if related else ""
        app_text = f" Students use it in {applications[0]}." if applications else ""
        if "math" in subject:
            return f"Understanding {concept} helps students reason quantitatively instead of only memorising steps.{related_text}{app_text}"
        if "science" in subject:
            return f"Understanding {concept} helps students explain observations, causes, and relationships in the natural world.{related_text}{app_text}"
        if "social" in subject or any(term in chapter for term in ("history", "government", "democracy", "resource")):
            return f"Understanding {concept} helps students connect textbook ideas with society, places, and public life.{related_text}{app_text}"
        return f"Understanding {concept} helps students connect the lesson to other ideas and real situations.{related_text}{app_text}"

    @staticmethod
    def _formula_context(rows: list[dict[str, Any]], concept: EducationalConcept) -> list[dict[str, Any]]:
        formulas: list[dict[str, Any]] = []
        concept_formula_keys = {item.lower() for item in concept.formulas}
        for row in rows:
            metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
            for info in metadata.get("formula_intelligence") or []:
                if not isinstance(info, dict):
                    continue
                formula = str(info.get("formula") or "")
                if concept_formula_keys and formula.lower() not in concept_formula_keys and concept.name.lower() not in str(info).lower():
                    continue
                formulas.append(info)
        return formulas[:5]


def top_terms(text: str, limit: int = 12) -> list[str]:
    counts = Counter(
        token.lower()
        for token in WORD_RE.findall(text or "")
        if len(token) >= 4 and token.lower() not in STOP_TERMS and not token.isdigit()
    )
    return [term for term, _count in counts.most_common(limit)]


def unique(values: list[Any]) -> list[Any]:
    seen: set[str] = set()
    output: list[Any] = []
    for value in values:
        if value is None:
            continue
        if isinstance(value, str):
            value = value.strip()
            if not value:
                continue
            key = value.lower()
        else:
            key = str(value)
        if key in seen:
            continue
        seen.add(key)
        output.append(value)
    return output


def percent(value: int, total: int) -> float:
    if total == 0:
        return 100.0
    return round(100.0 * value / total, 2)


def first_metadata(rows: list[dict[str, Any]]) -> dict[str, Any]:
    for row in rows:
        metadata = row.get("metadata")
        if isinstance(metadata, dict):
            return metadata
    return {}


def cleanup_phrase(value: str) -> str:
    value = re.sub(r"\b(before learning|you should know|depends on|based on)\b", "", value, flags=re.I)
    return re.sub(r"\s+", " ", value).strip(" .,:;").title()


def split_sentences(text: str) -> list[str]:
    return [item.strip() for item in re.split(r"(?<=[.!?])\s+", text or "") if item.strip()]


def first_explanatory_sentence(text: str) -> str:
    for sentence in split_sentences(text):
        lowered = sentence.lower()
        if any(marker in lowered for marker in ("because", "therefore", "means", "helps", "relationship", "process", "reason")):
            return sentence[:500]
    sentences = split_sentences(text)
    return sentences[0][:500] if sentences else ""


def first_example(text: str) -> str:
    for sentence in split_sentences(text):
        lowered = sentence.lower()
        if any(marker in lowered for marker in ("for example", "example", "suppose", "consider", "if ", "when ")):
            return sentence[:500]
    return ""
