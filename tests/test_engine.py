from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from domain import ConflictError, ValidationError
from engine import LedgerEngine
from store import JsonStore

T0 = datetime(2026, 9, 1, 9, 0, 0, tzinfo=timezone.utc)


def make_engine():
    clock = {"now": T0}
    engine = LedgerEngine(JsonStore(None), now_fn=lambda: clock["now"])
    return engine, clock


def seed_player_with_policies(engine, **overrides):
    player = engine.create_player({"name": "张三", "team": "一队"})
    course = engine.create_course({"player_id": player["id"], "diagnosis": "左膝前交叉韧带损伤"})
    insurance = engine.create_policy({
        "player_id": player["id"],
        "payer_type": "insurance",
        "name": "团体商业医疗2026",
        "effective_from": "2026-01-01",
        "effective_to": "2026-12-31",
        "deductible": "100.00",
        "annual_limit": "10000.00",
        "coverage_ratio": "0.8",
        "covered_categories": ["rehab"],
        "excluded_categories": ["wellness"],
        "preauth_required_categories": overrides.get("preauth_required", []),
    })
    club = engine.create_policy({
        "player_id": player["id"],
        "payer_type": "club",
        "name": "俱乐部康复福利",
        "effective_from": "2026-01-01",
        "effective_to": "2026-12-31",
        "deductible": "0",
        "annual_limit": "500.00",
        "coverage_ratio": "0.5",
        "covered_categories": ["rehab"],
    })
    engine.create_prescription({
        "course_id": course["id"],
        "items": [
            {"service_code": "PT-KNEE-01", "category": "rehab", "unit_price": "1000.00"},
            {"service_code": "SPA-01", "category": "wellness", "unit_price": "300.00"},
        ],
    })
    return player, course, insurance, club


def submit_line(engine, course, receipt, amount="1000.00", code="PT-KNEE-01",
                service_date="2026-09-01"):
    line = {
        "service_code": code,
        "quantity": "1",
        "unit_price": amount,
        "service_date": service_date,
    }
    if receipt is not None:
        line["receipt_id"] = receipt["id"]
    return engine.submit_claim({"course_id": course["id"], "lines": [line]})


def make_receipt(engine, player, receipt_no="R-001"):
    return engine.register_receipt({
        "player_id": player["id"],
        "receipt_no": receipt_no,
        "provider": "运动康复中心",
        "image_uri": "vault://original-1.tif",
    })


def totals_by_bucket(claim):
    return claim["summary"]["totals"]


class WaterfallSplitTest(unittest.TestCase):
    def test_insurance_then_club_then_personal(self):
        engine, _ = make_engine()
        player, course, insurance, club = seed_player_with_policies(engine)
        receipt = make_receipt(engine, player)

        claim = submit_line(engine, course, receipt)
        totals = totals_by_bucket(claim)

        # 1000.00：保险先扣 100 免赔，按 80% 赔 720；余 280 进入俱乐部 50% 赔 140；
        # 剩余 140 个人承担
        self.assertEqual(72000, _bucket(claim, "reimbursable", "insurance"))
        self.assertEqual(14000, _bucket(claim, "reimbursable", "club"))
        self.assertEqual(14000, totals["personal"])
        self.assertEqual(0, totals["pending_documents"])
        self.assertEqual("approved", claim["status"])

    def test_deductible_accumulates_across_claims(self):
        engine, _ = make_engine()
        player, course, insurance, club = seed_player_with_policies(engine)
        submit_line(engine, course, make_receipt(engine, player, "R-001"))
        claim2 = submit_line(engine, course, make_receipt(engine, player, "R-002"))

        # 第二笔免赔额已扣完：保险 1000*0.8=800，俱乐部 200*0.5=100，个人 100
        self.assertEqual(80000, _bucket(claim2, "reimbursable", "insurance"))
        self.assertEqual(10000, _bucket(claim2, "reimbursable", "club"))
        self.assertEqual(10000, totals_by_bucket(claim2)["personal"])

        view = engine.coverage_view(player["id"], course["id"])
        ins_view = next(p for p in view["policies"] if p["policy_id"] == insurance["id"])
        self.assertEqual(10000, ins_view["deductible_used_cents"])
        self.assertEqual(0, ins_view["deductible_remaining_cents"])
        self.assertEqual(152000, ins_view["limit_used_cents"])
        self.assertEqual(1000000 - 152000, ins_view["limit_remaining_cents"])

    def test_annual_limit_caps_insurance(self):
        engine, _ = make_engine()
        player, course, insurance, club = seed_player_with_policies(engine)
        # 把保险额度用到只剩 50
        engine.store.data["policies"][insurance["id"]]["annual_limit_cents"] = 5000
        claim = submit_line(engine, course, make_receipt(engine, player))
        # 免赔 100 后 900*0.8=720，但额度只剩 5000 分
        self.assertEqual(5000, _bucket(claim, "reimbursable", "insurance"))

    def test_excluded_service_is_denied(self):
        engine, _ = make_engine()
        player, course, insurance, club = seed_player_with_policies(engine)
        claim = submit_line(engine, course, make_receipt(engine, player),
                            amount="300.00", code="SPA-01")
        totals = totals_by_bucket(claim)
        self.assertEqual(30000, totals["denied"])
        self.assertEqual("denied", claim["status"])
        denial = claim["lines"][0]["allocations"][0]
        self.assertEqual("service_excluded", denial["reason"])

    def test_item_outside_plan_is_denied(self):
        engine, _ = make_engine()
        player, course, insurance, club = seed_player_with_policies(engine)
        claim = submit_line(engine, course, make_receipt(engine, player), code="PT-OTHER")
        self.assertEqual("denied", claim["status"])
        self.assertEqual("not_in_prescription",
                         claim["lines"][0]["allocations"][0]["reason"])


class DuplicateClaimTest(unittest.TestCase):
    def test_same_receipt_rejected_with_original(self):
        engine, _ = make_engine()
        player, course, insurance, club = seed_player_with_policies(engine)
        receipt = make_receipt(engine, player)
        first = submit_line(engine, course, receipt)

        with self.assertRaises(ConflictError) as ctx:
            submit_line(engine, course, receipt)
        payload = ctx.exception.payload
        self.assertEqual("duplicate_claim", payload["error"])
        self.assertEqual(first["id"], payload["original_claim_id"])
        self.assertEqual("approved", payload["status"])
        self.assertEqual(receipt["id"], payload["receipt_id"])

    def test_same_receipt_twice_within_one_claim_rejected(self):
        engine, _ = make_engine()
        player, course, insurance, club = seed_player_with_policies(engine)
        receipt = make_receipt(engine, player)
        with self.assertRaises(ValidationError):
            engine.submit_claim({
                "course_id": course["id"],
                "lines": [
                    {"service_code": "PT-KNEE-01", "unit_price": "100.00",
                     "service_date": "2026-09-01", "receipt_id": receipt["id"]},
                    {"service_code": "PT-KNEE-01", "unit_price": "200.00",
                     "service_date": "2026-09-02", "receipt_id": receipt["id"]},
                ],
            })

    def test_same_external_ref_rejected(self):
        engine, _ = make_engine()
        player, course, insurance, club = seed_player_with_policies(engine)
        first = engine.submit_claim({
            "course_id": course["id"],
            "external_ref": "FD-2026-0001",
            "lines": [{"service_code": "PT-KNEE-01", "unit_price": "100.00",
                       "service_date": "2026-09-01",
                       "receipt_id": make_receipt(engine, player, "R-101")["id"]}],
        })
        with self.assertRaises(ConflictError) as ctx:
            engine.submit_claim({
                "course_id": course["id"],
                "external_ref": "FD-2026-0001",
                "lines": [{"service_code": "PT-KNEE-01", "unit_price": "100.00",
                           "service_date": "2026-09-01",
                           "receipt_id": make_receipt(engine, player, "R-102")["id"]}],
            })
        self.assertEqual(first["id"], ctx.exception.payload["original_claim_id"])
        self.assertEqual("approved", ctx.exception.payload["status"])


class ReceiptVersionTest(unittest.TestCase):
    def test_supplement_does_not_overwrite_original(self):
        engine, _ = make_engine()
        player = engine.create_player({"name": "李四"})
        receipt = make_receipt(engine, player)

        updated = engine.add_receipt_version(receipt["id"], {
            "image_uri": "vault://supplement-2.tif",
            "uploaded_by": "supplier-9",
            "note": "补传清晰版",
        })
        self.assertEqual(2, len(updated["versions"]))
        self.assertEqual("vault://original-1.tif", updated["versions"][0]["image_uri"])
        self.assertEqual("original", updated["versions"][0]["kind"])
        self.assertEqual("supplement", updated["versions"][1]["kind"])
        self.assertEqual("supplier-9", updated["versions"][1]["uploaded_by"])

    def test_same_receipt_no_registered_twice_conflicts(self):
        engine, _ = make_engine()
        player = engine.create_player({"name": "李四"})
        first = make_receipt(engine, player)
        with self.assertRaises(ConflictError) as ctx:
            make_receipt(engine, player)
        self.assertEqual(first["id"], ctx.exception.payload["original_receipt_id"])


class PreAuthorizationTest(unittest.TestCase):
    def test_missing_preauth_goes_pending_then_supplement_approves(self):
        engine, clock = make_engine()
        player, course, insurance, club = seed_player_with_policies(
            engine, preauth_required=["rehab"])
        claim = submit_line(engine, course, make_receipt(engine, player))

        self.assertEqual("needs_documents", claim["status"])
        self.assertEqual(100000, totals_by_bucket(claim)["pending_documents"])
        self.assertEqual("pre_authorization_missing",
                         claim["lines"][0]["allocations"][0]["reason"])
        self.assertIsNotNone(claim["supplement_deadline"])

        resolved = engine.supplement_claim(claim["id"], {
            "uploaded_by": "supplier-9",
            "pre_authorization": {
                "policy_id": insurance["id"],
                "categories": ["rehab"],
                "approved_amount": "5000.00",
                "valid_from": "2026-08-01",
                "valid_to": "2026-12-31",
                "reference": "PA-7788",
            },
        })
        self.assertEqual("approved", resolved["status"])
        self.assertEqual(72000, _bucket(resolved, "reimbursable", "insurance"))
        self.assertEqual(0, totals_by_bucket(resolved)["pending_documents"])
        # 原待补件决定保留为已解决，历史不丢失
        resolved_allocs = [a for a in resolved["lines"][0]["allocations"] if a["resolved"]]
        self.assertEqual(1, len(resolved_allocs))

    def test_expired_preauth_leaves_decision_point(self):
        engine, clock = make_engine()
        player, course, insurance, club = seed_player_with_policies(
            engine, preauth_required=["rehab"])
        engine.create_pre_authorization({
            "course_id": course["id"],
            "policy_id": insurance["id"],
            "categories": ["rehab"],
            "approved_amount": "5000.00",
            "valid_from": "2026-01-01",
            "valid_to": "2026-06-30",
        })
        claim = submit_line(engine, course, make_receipt(engine, player),
                            service_date="2026-09-01")
        self.assertEqual("needs_documents", claim["status"])
        self.assertEqual("pre_authorization_expired",
                         claim["lines"][0]["allocations"][0]["reason"])

        events = engine.list_events("claim", claim["id"])
        expired = [e for e in events if e["type"] == "pre_authorization_expired"]
        self.assertEqual(1, len(expired))
        self.assertEqual("2026-09-01T09:00:00Z", expired[0]["detail"]["decided_at"])
        self.assertEqual(insurance["id"], expired[0]["detail"]["policy_id"])


class ReceiptMissingTest(unittest.TestCase):
    def test_missing_receipt_pending_then_linked(self):
        engine, _ = make_engine()
        player, course, insurance, club = seed_player_with_policies(engine)
        claim = submit_line(engine, course, None)
        self.assertEqual("needs_documents", claim["status"])
        self.assertEqual("receipt_missing",
                         claim["lines"][0]["allocations"][0]["reason"])

        receipt = make_receipt(engine, player)
        resolved = engine.supplement_claim(claim["id"], {
            "receipt_links": [{"line_id": "ln_1", "receipt_id": receipt["id"]}],
        })
        self.assertEqual("approved", resolved["status"])
        self.assertEqual(72000, _bucket(resolved, "reimbursable", "insurance"))


class PlanChangeTest(unittest.TestCase):
    def test_revision_leaves_decision_point_and_old_items_denied(self):
        engine, clock = make_engine()
        player, course, insurance, club = seed_player_with_policies(engine)
        prescription = engine._active_prescription(course["id"])

        clock["now"] = T0 + timedelta(days=7)
        engine.revise_prescription(prescription["id"], {
            "reason": "复查后去除水疗，改为力量训练",
            "items": [{"service_code": "PT-KNEE-02", "category": "rehab",
                       "unit_price": "800.00"}],
        })
        events = [e for e in engine.list_events("course", course["id"])
                  if e["type"] == "plan_changed"]
        self.assertEqual(1, len(events))
        self.assertEqual("2026-09-08T09:00:00Z", events[0]["detail"]["decided_at"])
        self.assertEqual(prescription["id"], events[0]["detail"]["from_prescription_id"])

        claim = submit_line(engine, course, make_receipt(engine, player), code="PT-KNEE-01")
        self.assertEqual("denied", claim["status"])
        self.assertEqual("not_in_prescription",
                         claim["lines"][0]["allocations"][0]["reason"])


class PolicyYearSwitchTest(unittest.TestCase):
    def test_cross_year_switch_leaves_decision_point(self):
        engine, clock = make_engine()
        player, course, insurance, club = seed_player_with_policies(engine)
        submit_line(engine, course, make_receipt(engine, player, "R-201"),
                    service_date="2026-06-01")

        insurance_2027 = engine.create_policy({
            "player_id": player["id"],
            "payer_type": "insurance",
            "name": "团体商业医疗2027",
            "effective_from": "2027-01-01",
            "effective_to": "2027-12-31",
            "deductible": "0",
            "annual_limit": "10000.00",
            "coverage_ratio": "0.9",
            "covered_categories": ["rehab"],
        })
        clock["now"] = datetime(2027, 1, 5, 9, 0, 0, tzinfo=timezone.utc)
        claim = submit_line(engine, course, make_receipt(engine, player, "R-202"),
                            service_date="2027-01-03")

        self.assertEqual(90000, _bucket(claim, "reimbursable", "insurance"))
        events = [e for e in engine.list_events("course", course["id"])
                  if e["type"] == "policy_year_switched"]
        self.assertEqual(1, len(events))
        self.assertEqual(insurance["id"], events[0]["detail"]["from_policy_id"])
        self.assertEqual(insurance_2027["id"], events[0]["detail"]["to_policy_id"])
        self.assertEqual("2027-01-05T09:00:00Z", events[0]["detail"]["decided_at"])


class FollowUpTest(unittest.TestCase):
    def test_deadline_arrival_flags_case_once(self):
        engine, clock = make_engine()
        player, course, insurance, club = seed_player_with_policies(engine)
        claim = submit_line(engine, course, None)  # 缺收据 -> 待补件

        self.assertEqual([], engine.scan_follow_ups())

        clock["now"] = T0 + timedelta(days=31)
        flagged = engine.scan_follow_ups()
        self.assertEqual(1, len(flagged))
        self.assertEqual(claim["id"], flagged[0]["claim_id"])
        self.assertEqual("receipt_missing", flagged[0]["pending"][0]["reason"])

        # 再次扫描不重复标记
        engine.scan_follow_ups()
        events = [e for e in engine.list_events("claim", claim["id"])
                  if e["type"] == "follow_up_flagged"]
        self.assertEqual(1, len(events))

        # 补件完成后自动解除跟进
        receipt = make_receipt(engine, player)
        engine.supplement_claim(claim["id"], {
            "receipt_links": [{"line_id": "ln_1", "receipt_id": receipt["id"]}],
        })
        self.assertEqual([], engine.scan_follow_ups())


class PaymentBatchTest(unittest.TestCase):
    def test_batch_trace_and_paid_transition(self):
        engine, _ = make_engine()
        player, course, insurance, club = seed_player_with_policies(engine)
        receipt = make_receipt(engine, player)
        claim = submit_line(engine, course, receipt)

        ins_batch = engine.create_payment_batch({"policy_id": insurance["id"]})
        self.assertEqual(72000, ins_batch["total_cents"])
        # 俱乐部部分未付，申请还不是 paid
        self.assertEqual("approved", engine.get_claim(claim["id"])["status"])

        club_batch = engine.create_payment_batch({"payer_type": "club"})
        self.assertEqual(14000, club_batch["total_cents"])
        self.assertEqual("paid", engine.get_claim(claim["id"])["status"])

        # 会计反查：批次 -> 每张收据的责任依据
        trace = engine.trace_payment_batch(ins_batch["id"])
        self.assertEqual(1, len(trace["receipts"]))
        entry = trace["receipts"][0]
        self.assertEqual(receipt["id"], entry["receipt_id"])
        self.assertEqual("R-001", entry["receipt_no"])
        liability = entry["liabilities"][0]
        self.assertEqual(insurance["id"], liability["policy_id"])
        self.assertEqual("0.8", liability["coverage_ratio"])
        self.assertEqual(10000, liability["deductible_applied_cents"])
        self.assertEqual(1, liability["receipt_version"])
        self.assertEqual(claim["id"], liability["claim_id"])

        # 已付的不能重复付款
        with self.assertRaises(ValidationError):
            engine.create_payment_batch({"policy_id": insurance["id"]})

    def test_payout_event_recorded(self):
        engine, _ = make_engine()
        player, course, insurance, club = seed_player_with_policies(engine)
        submit_line(engine, course, make_receipt(engine, player))
        batch = engine.create_payment_batch({"payer_type": "insurance"})
        events = [e for e in engine.list_events("payment_batch", batch["id"])
                  if e["type"] == "payout"]
        self.assertEqual(1, len(events))
        self.assertEqual(72000, events[0]["detail"]["total_cents"])


def _bucket(claim, bucket, payer_type=None):
    total = 0
    for line in claim["lines"]:
        for allocation in line["allocations"]:
            if allocation.get("resolved") or allocation["bucket"] != bucket:
                continue
            if payer_type is not None and allocation["payer_type"] != payer_type:
                continue
            total += allocation["amount_cents"]
    return total


if __name__ == "__main__":
    unittest.main()
