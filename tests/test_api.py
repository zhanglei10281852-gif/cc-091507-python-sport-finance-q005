from __future__ import annotations

import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from app import create_server


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = create_server("127.0.0.1", 0, store_path=None)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def request(self, method, path, body=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read().decode("utf-8"))

    def test_health(self):
        status, payload = self.request("GET", "/health")
        self.assertEqual(200, status)
        self.assertEqual("ok", payload["status"])

    def test_unknown_route_is_404(self):
        status, payload = self.request("GET", "/nope")
        self.assertEqual(404, status)

    def test_invalid_json_is_400(self):
        url = f"http://127.0.0.1:{self.port}/players"
        req = urllib.request.Request(url, data=b"{not json", method="POST")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req)
        self.assertEqual(400, ctx.exception.code)

    def test_full_claim_flow_and_duplicate_409(self):
        status, player = self.request("POST", "/players", {"name": "王五"})
        self.assertEqual(201, status)
        status, course = self.request("POST", "/courses", {
            "player_id": player["id"], "diagnosis": "右膝半月板损伤"})
        self.assertEqual(201, status)
        status, policy = self.request("POST", "/policies", {
            "player_id": player["id"],
            "payer_type": "insurance",
            "name": "团体医疗",
            "effective_from": "2026-01-01",
            "effective_to": "2026-12-31",
            "deductible": "0",
            "annual_limit": "20000.00",
            "coverage_ratio": "0.9",
            "covered_categories": ["rehab"],
        })
        self.assertEqual(201, status)
        status, _ = self.request("POST", "/prescriptions", {
            "course_id": course["id"],
            "items": [{"service_code": "PT-01", "category": "rehab",
                       "unit_price": "500.00"}],
        })
        self.assertEqual(201, status)
        status, receipt = self.request("POST", "/receipts", {
            "player_id": player["id"], "receipt_no": "R-900", "provider": "康复中心",
            "image_uri": "vault://r900.tif"})
        self.assertEqual(201, status)

        claim_body = {
            "course_id": course["id"],
            "lines": [{"service_code": "PT-01", "unit_price": "500.00",
                       "service_date": "2026-09-10", "receipt_id": receipt["id"]}],
        }
        status, claim = self.request("POST", "/claims", claim_body)
        self.assertEqual(201, status)
        self.assertEqual("approved", claim["status"])
        self.assertEqual(45000, claim["summary"]["totals"]["reimbursable"])
        self.assertEqual(5000, claim["summary"]["totals"]["personal"])

        # 同一张收据重复索赔 -> 409 返回原申请及当前状态
        status, dup = self.request("POST", "/claims", claim_body)
        self.assertEqual(409, status)
        self.assertEqual("duplicate_claim", dup["error"])
        self.assertEqual(claim["id"], dup["original_claim_id"])
        self.assertEqual("approved", dup["status"])

        # 队医视角：免赔额与额度
        status, coverage = self.request(
            "GET", f"/players/{player['id']}/courses/{course['id']}/coverage")
        self.assertEqual(200, status)
        self.assertEqual(45000, coverage["policies"][0]["limit_used_cents"])

        # 付款批次与反查
        status, batch = self.request("POST", "/payment-batches",
                                     {"policy_id": policy["id"]})
        self.assertEqual(201, status)
        self.assertEqual(45000, batch["total_cents"])
        status, trace = self.request("GET", f"/payment-batches/{batch['id']}/trace")
        self.assertEqual(200, status)
        self.assertEqual(receipt["id"], trace["receipts"][0]["receipt_id"])
        self.assertEqual(policy["id"],
                         trace["receipts"][0]["liabilities"][0]["policy_id"])

        # 补件跟进列表可查询
        status, follow = self.request("GET", "/follow-ups")
        self.assertEqual(200, status)
        self.assertIn("flagged", follow)

        # 审计事件可查询
        status, events = self.request(
            "GET", f"/events?entity_type=claim&entity_id={claim['id']}")
        self.assertEqual(200, status)
        self.assertTrue(any(e["type"] == "claim_submitted" for e in events["events"]))

    def test_missing_field_is_400(self):
        status, payload = self.request("POST", "/players", {})
        self.assertEqual(400, status)
        self.assertEqual("validation_error", payload["error"])


if __name__ == "__main__":
    unittest.main()
