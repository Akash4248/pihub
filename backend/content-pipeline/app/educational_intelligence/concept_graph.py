from __future__ import annotations

import logging
from typing import Any

try:
    import networkx as nx
except ImportError:
    nx = None  # type: ignore

from .concept_models import EducationalConcept
from .educational_concept_validator import EducationalConceptValidator

logger = logging.getLogger(__name__)


class ConceptGraphBuilder:
    """NetworkX-powered Educational Concept DAG Builder.

    Constructs a Directed Acyclic Graph (DAG) of educational concepts,
    ensures topological validity (no circular prerequisites), and provides
    learning progression sequences for adaptive tutoring.
    """

    def __init__(self) -> None:
        self.validator = EducationalConceptValidator()

    def build(self, concepts: list[EducationalConcept]) -> dict[str, object]:
        original_count = len(concepts)
        concepts = self._clean_concepts(concepts)
        
        nodes: list[dict[str, Any]] = [
            {
                "concept_id": concept.concept_id,
                "concept": concept.name,
                "definition": concept.definition,
                "learning_objectives": concept.learning_objectives,
                "related": concept.related_concepts,
                "prerequisites": concept.prerequisites,
            }
            for concept in concepts
        ]
        
        edges: list[dict[str, Any]] = []
        concept_names = {concept.name for concept in concepts}
        for concept in concepts:
            for related in concept.related_concepts:
                if related in concept_names and related != concept.name:
                    edges.append({"source": concept.name, "target": related, "relationship": "related"})
            for prerequisite in concept.prerequisites:
                if prerequisite in concept_names and prerequisite != concept.name:
                    edges.append({"source": prerequisite, "target": concept.name, "relationship": "prerequisite"})

        # NetworkX DAG Analysis
        is_dag = True
        learning_sequence: list[str] = [node["concept"] for node in nodes]
        foundational: list[str] = []
        gateways: list[str] = []

        if nx is not None:
            try:
                g = nx.DiGraph()
                for node in nodes:
                    g.add_node(node["concept"], **node)
                
                # Add prerequisite edges into DAG
                prereq_graph = nx.DiGraph()
                for node in nodes:
                    prereq_graph.add_node(node["concept"])
                    
                for edge in edges:
                    g.add_edge(edge["source"], edge["target"], relationship=edge["relationship"])
                    if edge["relationship"] == "prerequisite":
                        prereq_graph.add_edge(edge["source"], edge["target"])

                # Check DAG and break cycles if any exist
                if not nx.is_directed_acyclic_graph(prereq_graph):
                    is_dag = False
                    cycles = list(nx.simple_cycles(prereq_graph))
                    logger.warning("Circular prerequisites detected in concept graph: %s; resolving...", cycles)
                    # Break the cycle by removing the feedback edge
                    for cycle in cycles:
                        if len(cycle) >= 2:
                            prereq_graph.remove_edge(cycle[-1], cycle[0])
                            edges = [e for e in edges if not (e["source"] == cycle[-1] and e["target"] == cycle[0] and e["relationship"] == "prerequisite")]
                    is_dag = nx.is_directed_acyclic_graph(prereq_graph)

                # Compute topological sort sequence
                if is_dag:
                    learning_sequence = list(nx.topological_sort(prereq_graph))

                # Identify foundational (in-degree == 0) and gateway (high out-degree) nodes
                for name in prereq_graph.nodes:
                    if prereq_graph.in_degree(name) == 0:
                        foundational.append(name)
                    if prereq_graph.out_degree(name) >= 2:
                        gateways.append(name)

            except Exception as exc:
                logger.debug("NetworkX analysis skipped due to error: %s", exc)

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
            "is_valid_dag": is_dag,
            "learning_sequence": learning_sequence,
            "foundational_concepts": foundational,
            "gateway_concepts": gateways,
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
