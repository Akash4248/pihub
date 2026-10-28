from __future__ import annotations

import os
import unittest
from fastapi.testclient import TestClient

os.environ["ALLOW_ANONYMOUS_DEV"] = "true"
os.environ["EMBEDDING_FALLBACK"] = "hash"

from app.main import app
from shared.security import get_internal_service_key


class ArtifactsEndpointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)
        self.headers = {"X-Internal-Service-Key": get_internal_service_key()}

    def test_post_artifacts_with_chunks(self) -> None:
        concept = (
            "Photosynthesis is the process by which green plants use sunlight, carbon dioxide, and water "
            "to prepare food. Chlorophyll helps leaves capture light energy. This process releases oxygen "
            "and supports life on Earth. Students should understand that plants make food in leaves and do "
            "not simply absorb food from soil. "
        ) * 5
        payload = {
            "grade": 8,
            "subject": "Science",
            "chapter": "Plant Life",
            "language": "en",
            "chunks": [
                {
                    "chunk_id": "c_1",
                    "text": concept,
                    "metadata": {"grade": 8, "subject": "Science", "chapter": "Plant Life", "topic": "Photosynthesis"},
                },
                {
                    "chunk_id": "c_2",
                    "text": "Exercise: What is photosynthesis? Explain with examples.",
                    "metadata": {"grade": 8, "subject": "Science", "chapter": "Plant Life"},
                },
            ],
        }
        response = self.client.post("/artifacts", json=payload, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertIn("chapter_id", data)
        self.assertIn("artifacts", data)
        self.assertIn("textbook", data)
        self.assertIn("quizzes", data["artifacts"])
        self.assertIn("flashcards", data["artifacts"])
        self.assertIn("summaries", data["artifacts"])
        self.assertIn("quality_gate", data)
        self.assertTrue(data["quality_gate"]["passed"])


if __name__ == "__main__":
    unittest.main()
