import unittest
from app import create_app
from app.database import db
from app.models import User, Journey, Bus

class TestAPI(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

    def test_auth_login_and_me(self):
        # Default seeded admin
        res = self.client.post("/api/auth/login", json={
            "username": "admin",
            "password": "admin123"
        })
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertIn("user", data)
        self.assertEqual(data["user"]["role"], "admin")

        # Verify /me
        res_me = self.client.get("/api/auth/me")
        self.assertEqual(res_me.status_code, 200)
        self.assertTrue(res_me.get_json()["authenticated"])

    def test_get_journeys(self):
        res = self.client.get("/api/journeys")
        self.assertEqual(res.status_code, 200)
        self.assertIsInstance(res.get_json(), list)

    def test_dashboard_stats(self):
        res = self.client.get("/api/dashboard/stats")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertIn("metrics", data)
        self.assertIn("total_identified", data["metrics"])

    def test_csv_export_endpoint(self):
        res = self.client.get("/api/reports/export.csv")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.mimetype, "text/csv")
        csv_text = res.get_data(as_text=True)
        # Check disclaimer and header
        self.assertIn("SMART BUS PASSENGER TRACKING", csv_text)
        self.assertIn("Person ID", csv_text)

if __name__ == "__main__":
    unittest.main()
