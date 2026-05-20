import tempfile
import unittest
from pathlib import Path

import db
import marketing_ai_service


class MarketingSmokeTest(unittest.TestCase):
    # marketing smoke tests
    @classmethod
    def setUpClass(cls):
        # test app setup
        cls.tmp = tempfile.TemporaryDirectory()
        db.DB_PATH = Path(cls.tmp.name) / "test.db"
        db.init_db()
        cls.user = db.create_user(
            phone="77000000099",
            password="pass",
            name="Smoke",
            email="smoke-test@example.com",
        )
        db.add_site_slot(cls.user["id"], 1000, "smoke")
        with db.get_conn() as c:
            c.execute("UPDATE users SET promo_credits=500 WHERE id=?", (cls.user["id"],))
        cls.site = db.create_site(
            cls.user["id"],
            "smoke-marketing",
            "Smoke Marketing",
            {"name": "Smoke", "services": "Service 1000", "city": "Almaty"},
            "generated_sites/smoke-marketing.html",
            0,
        )
        cls.sid = db.create_session(cls.user["id"])
        from fastapi.testclient import TestClient
        import main
        cls.client = TestClient(main.app)
        cls.cookies = {"sid": cls.sid}

    @classmethod
    def tearDownClass(cls):
        # cleanup
        cls.tmp.cleanup()

    def test_dashboard_marketing_renders(self):
        # dashboard render
        resp = self.client.get("/dashboard/marketing", cookies=self.cookies)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("marketing-shell", resp.text)

    def test_campaign_launch_deducts_marketing_credits(self):
        # campaign launch
        payload = {
            "site_id": self.site["id"],
            "goal": "more leads",
            "target_audience": "women 20-35",
            "location": "Almaty",
            "budget": 100,
            "budget_credits": 100,
            "platform": "instagram",
            "platforms": ["instagram"],
            "objective": "whatsapp clicks",
        }
        assistant = self.client.post("/api/marketing/assistant/message", json=payload, cookies=self.cookies)
        self.assertTrue(assistant.json()["ready"])
        created = self.client.post("/api/marketing/campaigns", json=payload, cookies=self.cookies)
        self.assertEqual(created.status_code, 200)
        self.assertTrue(created.json()["ok"])
        logs = self.client.get("/api/marketing/credits/logs", cookies=self.cookies)
        self.assertTrue(logs.json()["ok"])
        self.assertTrue(any(log["reason"] == "marketing_campaign_launch" for log in logs.json()["logs"]))

    def test_analytics_empty_state_is_honest(self):
        # analytics empty state
        resp = self.client.get("/api/marketing/analytics", cookies=self.cookies)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["ok"])
        self.assertIn("insights", resp.json()["analytics"])

    def test_ai_output_contract_rejects_malformed_content(self):
        # ai validation
        with self.assertRaises(ValueError):
            marketing_ai_service.validate_output({"summary": "bad", "content": {"ad_copy": "not-list"}})
        valid = marketing_ai_service.validate_output({"summary": "ok", "content": {}})
        self.assertEqual(valid["content"]["captions"], [])


if __name__ == "__main__":
    unittest.main()
