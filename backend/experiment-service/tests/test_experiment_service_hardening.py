import unittest
from fastapi.testclient import TestClient

from app.main import app


class ExperimentServiceHardeningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)

    def test_idor_protection_rejects_missing_user_id(self) -> None:
        response = self.client.get("/experiment-runs/student/student_101")
        self.assertEqual(response.status_code, 401)
        err = response.json().get("error", {})
        self.assertIn("Authentication required", err.get("message", ""))

    def test_idor_protection_rejects_unauthorized_student(self) -> None:
        headers = {"x-user-id": "evil_user", "x-user-role": "student"}
        response = self.client.get("/experiment-runs/student/student_101", headers=headers)
        self.assertEqual(response.status_code, 403)
        err = response.json().get("error", {})
        self.assertIn("Forbidden", err.get("message", ""))

    def test_idor_protection_allows_matching_student(self) -> None:
        headers = {"x-user-id": "student_101", "x-user-role": "student"}
        response = self.client.get("/experiment-runs/student/student_101", headers=headers)
        self.assertEqual(response.status_code, 200)

    def test_idor_protection_allows_admin_role(self) -> None:
        headers = {"x-user-id": "teacher_01", "x-user-role": "admin"}
        response = self.client.get("/experiment-runs/student/student_101", headers=headers)
        self.assertEqual(response.status_code, 200)

    def test_ai_route_alias_resolution(self) -> None:
        payload = {"description": "Simple Pendulum Experiment", "topic": "Pendulum Motion", "grade": 7, "subject": "science"}
        res1 = self.client.post("/ai/generate-experiment", json=payload)
        res2 = self.client.post("/ai/experiment/generate", json=payload)
        self.assertEqual(res1.status_code, 200)
        self.assertEqual(res2.status_code, 200)
        self.assertEqual(res1.json(), res2.json())

    def test_list_experiments_query_defaults(self) -> None:
        response = self.client.get("/experiments")
        self.assertEqual(response.status_code, 200)

    def test_classroom_session_auth_gate_rejects_anonymous(self) -> None:
        response = self.client.post("/classroom/sessions", json={"title": "Physics Lab", "teacher_id": "t1"})
        self.assertEqual(response.status_code, 401)

    def test_classroom_session_allows_teacher_header(self) -> None:
        headers = {"x-user-id": "t1", "x-user-role": "teacher"}
        response = self.client.post("/classroom/sessions", json={"title": "Physics Lab 2", "teacher_id": "t1"}, headers=headers)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("session_id", data)
        self.assertIn("assignment_session", data)
        self.assertIn("Warning", response.headers)


if __name__ == "__main__":
    unittest.main()
