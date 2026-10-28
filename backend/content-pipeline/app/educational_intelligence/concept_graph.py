from __future__ import annotations

from .concept_models import EducationalConcept
from .educational_concept_validator import EducationalConceptValidator


class ConceptGraphBuilder:
    def __init__(self) -> None:
        self.validator = EducationalConceptValidator()

    def build(self, concepts: list[EducationalConcept]) -> dict[str, object]:
        original_count = len(concepts)
        concepts = self._clean_concepts(concepts)
        nodes = [
            {
                "concept_id": concept.concept_id,
                "concept": concept.name,
                "definition": concept.definition,
                "learning_objectives": concept.learning_objectives,
                "related": concept.related_concepts,
            }
            for concept in concepts
        ]
        edges = []
        concept_names = {concept.name for concept in concepts}
        for concept in concepts:
            for related in concept.related_concepts:
                if related in concept_names:
                    edges.append({"source": concept.name, "target": related, "relationship": "related"})
            for prerequisite in concept.prerequisites:
                edges.append({"source": prerequisite, "target": concept.name, "relationship": "prerequisite"})
        return {
            "nodes": nodes,
            "edges": edges,
            "concept_count": len(nodes),
            "edge_count": len(edges),
            "cleanup": {
                "input_nodes": original_count,
                "output_nodes": len(nodes),
                "removed_nodes": original_count - len(nodes),
            },
            "concept_graph": [{"concept": node["concept"], "related": node["related"]} for node in nodes],
        }

    def _clean_concepts(self, concepts: list[EducationalConcept]) -> list[EducationalConcept]:
        cleaned: list[EducationalConcept] = []
        seen: set[str] = set()
        for concept in concepts:
            evidence = {
                "frequency": len(concept.source_chunk_ids),
                "has_definition": bool(concept.definition),
                "has_formula": bool(concept.formulas),
                "has_example": bool(concept.examples or concept.worked_examples),
                "text": " ".join([concept.definition, *concept.examples[:1], *concept.worked_examples[:1]]),
            }
            validation = self.validator.validate(concept.name, evidence)
            key = concept.name.lower().strip()
            if not validation.valid or key in seen:
                continue
            if not concept.related_concepts and len(concept.source_chunk_ids) < 2 and not concept.formulas and not concept.definition:
                continue
            cleaned.append(concept)
            seen.add(key)
        return cleaned
