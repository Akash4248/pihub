"""
SQLite Concept Graph & Dependency Exporter
Exports concept dependency nodes and edges into portable SQLite database files
for offline mobile consumption and Knowledge Gap analysis.
"""

import sqlite3
import json
import logging
from pathlib import Path
from typing import Dict, List, Any, Optional

logger = logging.getLogger(__name__)

class ConceptGraphExporter:
    """
    Exports structured concept graph and prerequisite dependencies into SQLite.
    """

    def export_concept_graph(
        self,
        output_db_path: str,
        subject: str = "Physics",
        grade: int = 11,
        concepts: Optional[List[Dict[str, Any]]] = None,
        prerequisites: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        """
        Creates or updates SQLite DB with `concept_nodes` and `concept_prerequisites` tables.
        """
        out_path = Path(output_db_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)

        conn = sqlite3.connect(str(out_path))
        cursor = conn.cursor()

        # 1. Create concept_nodes table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS concept_nodes (
                concept_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                subject TEXT NOT NULL,
                grade INTEGER NOT NULL,
                chapter_id TEXT,
                difficulty TEXT DEFAULT 'intermediate',
                summary TEXT,
                learning_objectives TEXT
            )
        """)

        # 2. Create concept_prerequisites table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS concept_prerequisites (
                source_concept_id TEXT NOT NULL,
                target_concept_id TEXT NOT NULL,
                relation_type TEXT DEFAULT 'PREREQUISITE',
                weight REAL DEFAULT 1.0,
                PRIMARY KEY (source_concept_id, target_concept_id),
                FOREIGN KEY (source_concept_id) REFERENCES concept_nodes(concept_id),
                FOREIGN KEY (target_concept_id) REFERENCES concept_nodes(concept_id)
            )
        """)

        # Use fallback seed concepts if none provided
        if not concepts:
            concepts = self._generate_default_concepts(subject, grade)
        if not prerequisites:
            prerequisites = self._generate_default_prerequisites(concepts)

        # Insert Concept Nodes
        for c in concepts:
            cursor.execute("""
                INSERT OR REPLACE INTO concept_nodes (
                    concept_id, name, subject, grade, chapter_id, difficulty, summary, learning_objectives
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                c["concept_id"],
                c["name"],
                c.get("subject", subject),
                c.get("grade", grade),
                c.get("chapter_id", f"class_{grade}_{subject.lower()}_ch1"),
                c.get("difficulty", "intermediate"),
                c.get("summary", ""),
                json.dumps(c.get("learning_objectives", [])),
            ))

        # Insert Concept Prerequisites
        for p in prerequisites:
            cursor.execute("""
                INSERT OR REPLACE INTO concept_prerequisites (
                    source_concept_id, target_concept_id, relation_type, weight
                ) VALUES (?, ?, ?, ?)
            """, (
                p["source_concept_id"],
                p["target_concept_id"],
                p.get("relation_type", "PREREQUISITE"),
                p.get("weight", 1.0),
            ))

        conn.commit()
        conn.close()

        logger.info(f"Exported concept graph DB with {len(concepts)} nodes and {len(prerequisites)} edges to {out_path}")
        return str(out_path)

    def _generate_default_concepts(self, subject: str, grade: int) -> List[Dict[str, Any]]:
        if subject.lower() == "physics":
            return [
                {
                    "concept_id": "c_vectors_101",
                    "name": "Vector Algebra & Resolution",
                    "chapter_id": f"class_{grade}_physics_ch1",
                    "difficulty": "basic",
                    "summary": "Addition, dot product, cross product, and components of physical vectors.",
                    "learning_objectives": ["Vector addition", "Scalar product", "Cross product"],
                },
                {
                    "concept_id": "c_kinematics_102",
                    "name": "Kinematics in 1D & 2D",
                    "chapter_id": f"class_{grade}_physics_ch1",
                    "difficulty": "intermediate",
                    "summary": "Displacement, velocity, acceleration, and projectile trajectory equations.",
                    "learning_objectives": ["Equations of motion", "Relative velocity", "Projectile motion"],
                },
                {
                    "concept_id": "c_newton_laws_103",
                    "name": "Newton's Laws of Motion",
                    "chapter_id": f"class_{grade}_physics_ch2",
                    "difficulty": "intermediate",
                    "summary": "Inertia, force equals mass times acceleration, action-reaction, and free body diagrams.",
                    "learning_objectives": ["Free body diagrams", "Friction", "Centripetal force"],
                },
                {
                    "concept_id": "c_work_energy_104",
                    "name": "Work-Energy Theorem & Conservation",
                    "chapter_id": f"class_{grade}_physics_ch3",
                    "difficulty": "advanced",
                    "summary": "Work done by conservative and non-conservative forces, potential energy curves.",
                    "learning_objectives": ["Work-Energy Theorem", "Conservation of Energy", "Power"],
                },
            ]
        else:
            return [
                {
                    "concept_id": f"c_{subject.lower()}_basic",
                    "name": f"Foundational {subject}",
                    "chapter_id": f"class_{grade}_{subject.lower()}_ch1",
                    "difficulty": "basic",
                    "summary": f"Basic core definitions and equations for {subject}.",
                    "learning_objectives": ["Fundamental definitions", "Base principles"],
                },
                {
                    "concept_id": f"c_{subject.lower()}_advanced",
                    "name": f"Applied {subject} Problem Solving",
                    "chapter_id": f"class_{grade}_{subject.lower()}_ch2",
                    "difficulty": "advanced",
                    "summary": f"Advanced problem solving methods in {subject}.",
                    "learning_objectives": ["Advanced problem solving", "Analytical formulations"],
                },
            ]

    def _generate_default_prerequisites(self, concepts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        edges = []
        for i in range(len(concepts) - 1):
            edges.append({
                "source_concept_id": concepts[i]["concept_id"],
                "target_concept_id": concepts[i + 1]["concept_id"],
                "relation_type": "PREREQUISITE",
                "weight": 1.0,
            })
        return edges
