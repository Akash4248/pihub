import unittest
import os
import tempfile
import sqlite3
from app.diksha_sunbird_ingestor import DikshaSunbirdIngestor
from app.ncert_qr_mapper import NCERTQRMapper
from app.concept_graph_exporter import ConceptGraphExporter

class TestDikshaAndConceptGraph(unittest.TestCase):

    def test_diksha_ingestor_fallback(self):
        ingestor = DikshaSunbirdIngestor()
        results = ingestor.search_content(board="CBSE", grade="Class 11", subject="Physics", topic="Binomial Theorem")
        self.assertGreater(len(results), 0)
        item = results[0]
        self.assertIn("content_id", item)
        self.assertIn("title", item)
        self.assertIn("learning_outcomes", item)

    def test_ncert_qr_mapper_parsing(self):
        mapper = NCERTQRMapper()
        parsed = mapper.parse_qr_code("11062CH01")
        self.assertTrue(parsed["valid"])
        self.assertEqual(parsed["grade_num"], 11)
        self.assertEqual(parsed["subject"], "Physics")
        self.assertEqual(parsed["chapter_num"], 1)

        mapped = mapper.map_qr_to_chapter("11062CH01")
        self.assertEqual(mapped["chapter_id"], "class_11_physics_ch1")
        self.assertIn("learning_outcomes", mapped)

    def test_concept_graph_sqlite_export(self):
        exporter = ConceptGraphExporter()
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = os.path.join(tmp_dir, "test_concept_graph.sqlite")
            res_path = exporter.export_concept_graph(db_path, subject="Physics", grade=11)
            self.assertTrue(os.path.exists(res_path))

            conn = sqlite3.connect(res_path)
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM concept_nodes")
            node_count = cursor.fetchone()[0]
            cursor.execute("SELECT COUNT(*) FROM concept_prerequisites")
            edge_count = cursor.fetchone()[0]
            conn.close()

            self.assertGreaterEqual(node_count, 4)
            self.assertGreaterEqual(edge_count, 3)

if __name__ == "__main__":
    unittest.main()
