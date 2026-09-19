from __future__ import annotations

import http.client
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from app import Api, create_server  # noqa: E402
from ledger import Ledger, Store  # noqa: E402

CATALOG = json.loads((ROOT / "reference" / "domain.json").read_text(encoding="utf-8"))


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        store = Store(Path(cls.tmp.name) / "ledger.json")
        ledger = Ledger(store, catalog=CATALOG)
        cls.server = create_server("127.0.0.1", 0, api=Api(ledger))
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def request(self, method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        payload = json.dumps(body) if body is not None else None
        headers = {"Content-Type": "application/json"} if body is not None else {}
        conn.request(method, path, body=payload, headers=headers)
        resp = conn.getresponse()
        data = json.loads(resp.read().decode("utf-8"))
        conn.close()
        return resp.status, data

    def test_health(self):
        status, data = self.request("GET", "/health")
        self.assertEqual(200, status)
        self.assertEqual("ok", data["status"])

    def test_unknown_route_404(self):
        status, data = self.request("GET", "/nope")
        self.assertEqual(404, status)
        self.assertEqual("route_not_found", data["error"]["code"])

    def test_invalid_json_400(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("POST", "/players", body="{bad", headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        data = json.loads(resp.read().decode("utf-8"))
        conn.close()
        self.assertEqual(400, resp.status)
        self.assertEqual("invalid_json", data["error"]["code"])

    def test_end_to_end_flow(self):
        status, data = self.request("POST", "/players", {"player_id": "ap1", "name": "王五", "team": "一线队"})
        self.assertEqual(201, status)

        status, data = self.request(
            "POST",
            "/courses",
            {"course_id": "ac1", "player_id": "ap1", "diagnosis": "右膝半月板损伤", "started_at": "2026-02-01"},
        )
        self.assertEqual(201, status)

        status, data = self.request(
            "POST",
            "/prescriptions",
            {
                "prescription_id": "arx1",
                "course_id": "ac1",
                "items": [{"service_code": "PT-KNEE-MANUAL", "quantity": 10}],
                "effective_from": "2026-02-01",
            },
        )
        self.assertEqual(201, status)

        status, data = self.request(
            "POST",
            "/policies",
            {
                "policy_id": "A-COMM",
                "payer_type": "commercial_insurance",
                "payer_name": "测试商保",
                "policy_year": "2026",
                "effective_from": "2026-01-01",
                "effective_to": "2026-12-31",
                "deductible": "200.00",
                "annual_limit": "10000.00",
                "rules": [{"service_code": "*", "ratio": "0.8", "required_documents": ["invoice_image"]}],
            },
        )
        self.assertEqual(201, status)

        status, data = self.request(
            "POST",
            "/receipts",
            {
                "receipt_id": "ar1",
                "player_id": "ap1",
                "course_id": "ac1",
                "vendor": "康复中心",
                "receipt_no": "AR-001",
                "service_date": "2026-02-10",
                "lines": [{"service_code": "PT-KNEE-MANUAL", "quantity": 2}],
                "documents": ["invoice_image"],
                "image": {"image_ref": "img/ar1.tif"},
            },
        )
        self.assertEqual(201, status)
        self.assertFalse(data["duplicate"])
        self.assertEqual("960.00", data["receipt"]["lines"][0]["amount"])

        # 重复提交同一票据 → 返回原收据
        status, data = self.request(
            "POST",
            "/receipts",
            {
                "player_id": "ap1",
                "course_id": "ac1",
                "vendor": "康复中心",
                "receipt_no": "AR-001",
                "service_date": "2026-02-10",
                "lines": [{"service_code": "PT-KNEE-MANUAL", "quantity": 2}],
            },
        )
        self.assertEqual(200, status)
        self.assertTrue(data["duplicate"])
        self.assertEqual("ar1", data["receipt"]["receipt_id"])

        # 提交理赔：960 = 免赔200 流出 + 760×0.8=608 可报 + 152 自付 → 个人 352
        status, data = self.request("POST", "/claims", {"receipt_id": "ar1"})
        self.assertEqual(201, status)
        self.assertEqual("approved", data["status"])
        self.assertEqual("608.00", data["claim"]["totals"]["reimbursable"])
        self.assertEqual("352.00", data["claim"]["totals"]["personal"])
        claim_id = data["claim"]["claim_id"]

        # 重复索赔 → 返回原申请及当前状态
        status, data = self.request("POST", "/claims", {"receipt_id": "ar1"})
        self.assertEqual(200, status)
        self.assertTrue(data["duplicate"])
        self.assertEqual(claim_id, data["claim"]["claim_id"])
        self.assertEqual("approved", data["status"])

        # 队医视图：累计免赔额与剩余额度
        status, data = self.request("GET", "/players/ap1/courses/ac1/summary")
        self.assertEqual(200, status)
        policy = data["policies"][0]
        self.assertEqual("200.00", policy["deductible_used"])
        self.assertEqual("0.00", policy["deductible_remaining"])
        self.assertEqual("608.00", policy["limit_used"])
        self.assertEqual("9392.00", policy["limit_remaining"])

        # 会计：付款批次与反查
        status, data = self.request("POST", "/payment-batches", {"policy_id": "A-COMM"})
        self.assertEqual(201, status)
        batch_id = data["batch"]["batch_id"]
        self.assertEqual("608.00", data["batch"]["total"])

        status, data = self.request("GET", f"/payment-batches/{batch_id}/trace")
        self.assertEqual(200, status)
        line = data["lines"][0]
        self.assertEqual("AR-001", line["receipt_no"])
        self.assertEqual("608.00", line["portions"][0]["amount"])
        self.assertEqual("A-COMM", line["portions"][0]["basis"]["policy_id"])

        # 已支付后再次结算 → 409
        status, data = self.request("POST", "/payment-batches", {"policy_id": "A-COMM"})
        self.assertEqual(409, status)

    def test_follow_up_flow_via_api(self):
        self.request("POST", "/players", {"player_id": "ap2", "name": "赵六"})
        self.request(
            "POST",
            "/courses",
            {"course_id": "ac2", "player_id": "ap2", "diagnosis": "左膝扭伤", "started_at": "2026-03-01"},
        )
        self.request(
            "POST",
            "/prescriptions",
            {
                "course_id": "ac2",
                "items": [{"service_code": "PT-KNEE-EXERCISE", "quantity": 5}],
                "effective_from": "2026-03-01",
            },
        )
        self.request(
            "POST",
            "/policies",
            {
                "policy_id": "A-COMM-2",
                "payer_type": "commercial_insurance",
                "payer_name": "测试商保",
                "policy_year": "2026",
                "effective_from": "2026-01-01",
                "effective_to": "2026-12-31",
                "deductible": "0.00",
                "annual_limit": "10000.00",
                "rules": [
                    {
                        "service_code": "PT-KNEE-EXERCISE",
                        "ratio": "1",
                        "required_documents": ["invoice_image", "itemized_bill"],
                    }
                ],
            },
        )
        self.request(
            "POST",
            "/receipts",
            {
                "receipt_id": "ar2",
                "player_id": "ap2",
                "course_id": "ac2",
                "vendor": "康复中心",
                "receipt_no": "AR-002",
                "service_date": "2026-03-05",
                "lines": [{"service_code": "PT-KNEE-EXERCISE", "quantity": 1}],
                "documents": ["invoice_image"],
                "image": {"image_ref": "img/ar2-orig.tif"},
            },
        )
        status, data = self.request("POST", "/claims", {"receipt_id": "ar2"})
        self.assertEqual(201, status)
        self.assertEqual("needs_documents", data["status"])
        claim_id = data["claim"]["claim_id"]

        # 以未来时点触发补件到期标记
        status, data = self.request(
            "POST", "/maintenance/sweep", {"as_of": "2027-01-01T00:00:00+00:00"}
        )
        self.assertEqual(200, status)
        self.assertIn(claim_id, data["flagged"])

        status, data = self.request("GET", "/follow-ups")
        self.assertEqual(200, status)
        self.assertEqual(claim_id, data["follow_ups"][0]["claim_id"])
        self.assertEqual(["itemized_bill"], data["follow_ups"][0]["missing_documents"])

        # 补件（含供应商补传影像）后解除
        status, data = self.request(
            "POST",
            f"/claims/{claim_id}/supplements",
            {
                "documents": ["itemized_bill"],
                "image": {"image_ref": "img/ar2-fix.tif"},
            },
        )
        self.assertEqual(200, status)
        self.assertEqual("approved", data["claim"]["status"])

        status, data = self.request("GET", "/receipts/ar2")
        self.assertEqual(2, len(data["receipt"]["images"]))
        self.assertEqual("original", data["receipt"]["images"][0]["kind"])
        self.assertEqual("supplement", data["receipt"]["images"][1]["kind"])


if __name__ == "__main__":
    unittest.main()
