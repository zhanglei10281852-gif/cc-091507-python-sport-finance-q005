from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ledger import ConflictError, Ledger, NotFoundError, Store, ValidationError  # noqa: E402

CATALOG = json.loads((ROOT / "reference" / "domain.json").read_text(encoding="utf-8"))

T0 = datetime(2026, 1, 10, 9, 0, 0, tzinfo=timezone.utc)


class FakeClock:
    def __init__(self, start=T0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, **kwargs):
        self.now += timedelta(**kwargs)


def make_ledger(clock=None):
    tmp = tempfile.TemporaryDirectory()
    store = Store(Path(tmp.name) / "ledger.json")
    ledger = Ledger(store, clock=clock or FakeClock(), catalog=CATALOG)
    ledger._tmp = tmp  # 防止临时目录被回收
    return ledger


def seed_world(ledger, *, plan_items=None, effective_from="2026-01-01"):
    """建立球员、疗程、处方与商业/俱乐部两张保单。"""
    ledger.add_player(player_id="p1", name="张三", team="一线队")
    ledger.add_course(
        course_id="c1", player_id="p1", diagnosis="左膝前交叉韧带损伤", started_at="2026-01-02"
    )
    ledger.add_prescription(
        prescription_id="rx1",
        course_id="c1",
        items=plan_items
        or [
            {"service_code": "PT-KNEE-EXERCISE", "quantity": 20},
            {"service_code": "MRI-KNEE", "quantity": 2},
        ],
        effective_from=effective_from,
    )
    ledger.add_policy(
        policy_id="COMM-2026",
        payer_type="commercial_insurance",
        payer_name="安联商业医疗",
        policy_year="2026",
        effective_from="2026-01-01",
        effective_to="2026-12-31",
        deductible="500.00",
        annual_limit="20000.00",
        rules=[
            {
                "service_code": "PT-KNEE-EXERCISE",
                "ratio": "0.8",
                "required_documents": ["invoice_image", "itemized_bill"],
            },
            {
                "service_code": "MRI-KNEE",
                "ratio": "0.8",
                "requires_preauth": True,
                "required_documents": ["invoice_image"],
            },
        ],
    )
    ledger.add_policy(
        policy_id="CLUB-2026",
        payer_type="club_benefit",
        payer_name="俱乐部医疗基金",
        policy_year="2026",
        effective_from="2026-01-01",
        effective_to="2026-12-31",
        deductible="0.00",
        annual_limit="5000.00",
        rules=[{"service_code": "*", "ratio": "0.5"}],
    )


def post_receipt(
    ledger, *, lines, documents=None, receipt_no="R-001", service_date="2026-01-12", image=None
):
    return ledger.add_receipt(
        player_id="p1",
        course_id="c1",
        vendor="康复中心",
        receipt_no=receipt_no,
        service_date=service_date,
        lines=lines,
        documents=documents if documents is not None else ["invoice_image", "itemized_bill"],
        image=image if image is not None else {"image_ref": "img/original-1.tif", "uploaded_by": "康复中心"},
    )["receipt"]


class AdjudicationTest(unittest.TestCase):
    def setUp(self):
        self.ledger = make_ledger()
        seed_world(self.ledger)

    def test_split_follows_payer_order_and_conserves_amount(self):
        receipt = post_receipt(
            self.ledger, lines=[{"service_code": "PT-KNEE-EXERCISE", "quantity": 10}]
        )
        claim = self.ledger.submit_claim(receipt.receipt_id)["claim"]

        # 3600 = 商业(免赔500 流出, 余3100×0.8=2480 可报, 自付620 流出)
        #        俱乐部(1120×0.5=560 可报, 560 流出) → 个人 560
        self.assertEqual("approved", claim.status)
        self.assertEqual("3040.00", claim.totals["reimbursable"])
        self.assertEqual("560.00", claim.totals["personal"])
        self.assertEqual("0.00", claim.totals["pending_documents"])
        self.assertEqual("0.00", claim.totals["denied"])
        total = sum(Decimal(v) for v in claim.totals.values())
        self.assertEqual(Decimal("3600.00"), total)

        by_policy = {}
        for p in claim.portions:
            by_policy.setdefault(p.policy_id, []).append(p)
        comm = by_policy["COMM-2026"][0]
        self.assertEqual("2480.00", comm.amount)
        self.assertEqual("0.8", comm.basis["ratio"])
        self.assertEqual("500.00", comm.basis["deductible_applied"])
        club = by_policy["CLUB-2026"][0]
        self.assertEqual("560.00", club.amount)
        personal = by_policy[None][0]
        self.assertEqual("personal_account", personal.payer_type)

    def test_deductible_accumulates_across_receipts(self):
        r1 = post_receipt(self.ledger, lines=[{"service_code": "PT-KNEE-EXERCISE", "quantity": 1}])
        self.ledger.submit_claim(r1.receipt_id)
        r2 = post_receipt(
            self.ledger,
            lines=[{"service_code": "PT-KNEE-EXERCISE", "quantity": 1}],
            receipt_no="R-002",
        )
        claim2 = self.ledger.submit_claim(r2.receipt_id)["claim"]
        comm = [p for p in claim2.portions if p.policy_id == "COMM-2026"][0]
        # 第一张收据 360 元已全部计入免赔额，第二张只剩 140 免赔空间
        self.assertEqual("140.00", comm.basis["deductible_applied"])

        summary = self.ledger.course_summary("p1", "c1")
        comm_view = [p for p in summary["policies"] if p["policy_id"] == "COMM-2026"][0]
        self.assertEqual("500.00", comm_view["deductible_used"])
        self.assertEqual("0.00", comm_view["deductible_remaining"])
        self.assertEqual("20000.00", comm_view["annual_limit"])
        self.assertTrue(Decimal(comm_view["limit_remaining"]) < Decimal("20000.00"))

    def test_missing_documents_goes_pending_with_deadline(self):
        receipt = post_receipt(
            self.ledger,
            lines=[{"service_code": "PT-KNEE-EXERCISE", "quantity": 2}],
            documents=["invoice_image"],  # 缺 itemized_bill
        )
        claim = self.ledger.submit_claim(receipt.receipt_id)["claim"]
        # 720：免赔500 流出，余220×0.8=176 待补件；俱乐部 (500+44)×0.5=272 可报
        self.assertEqual("needs_documents", claim.status)
        self.assertEqual("176.00", claim.totals["pending_documents"])
        self.assertEqual("272.00", claim.totals["reimbursable"])
        self.assertIsNotNone(claim.supplement_deadline)

    def test_supplement_keeps_original_image_and_readjudicates(self):
        receipt = post_receipt(
            self.ledger,
            lines=[{"service_code": "PT-KNEE-EXERCISE", "quantity": 2}],
            documents=["invoice_image"],
        )
        claim = self.ledger.submit_claim(receipt.receipt_id)["claim"]
        self.assertEqual("needs_documents", claim.status)

        updated = self.ledger.submit_supplement(
            claim.claim_id,
            documents=["itemized_bill"],
            image={"image_ref": "img/supplement-1.tif", "uploaded_by": "康复中心"},
        )
        self.assertEqual("approved", updated.status)
        self.assertEqual("0.00", updated.totals["pending_documents"])
        self.assertIsNone(updated.supplement_deadline)

        images = self.ledger.get_receipt(receipt.receipt_id).images
        self.assertEqual(2, len(images))
        self.assertEqual("original", images[0].kind)
        self.assertEqual("img/original-1.tif", images[0].image_ref)  # 原始影像未被覆盖
        self.assertEqual("supplement", images[1].kind)
        self.assertEqual("img/supplement-1.tif", images[1].image_ref)

    def test_supplement_rejected_when_not_pending(self):
        receipt = post_receipt(
            self.ledger, lines=[{"service_code": "PT-KNEE-EXERCISE", "quantity": 1}]
        )
        claim = self.ledger.submit_claim(receipt.receipt_id)["claim"]
        with self.assertRaises(ConflictError):
            self.ledger.submit_supplement(claim.claim_id, documents=["itemized_bill"])

    def test_expired_preauth_denies_and_records_decision_event(self):
        self.ledger.add_preauthorization(
            preauth_id="pa1",
            policy_id="COMM-2026",
            course_id="c1",
            service_codes=["MRI-KNEE"],
            max_amount="3000.00",
            valid_from="2026-01-01",
            valid_until="2026-01-15",
        )
        receipt = post_receipt(
            self.ledger,
            lines=[{"service_code": "MRI-KNEE", "quantity": 1}],
            documents=["invoice_image"],
            service_date="2026-02-01",  # 预授权已过期
        )
        claim = self.ledger.submit_claim(receipt.receipt_id)["claim"]
        self.assertEqual("approved", claim.status)  # 俱乐部部分仍可报
        # 商业应承担部分 (1200-500免赔)×0.8=560 被拒
        self.assertEqual("560.00", claim.totals["denied"])
        denied = [p for p in claim.portions if p.category == "denied"][0]
        self.assertEqual("preauth_expired", denied.reason)
        self.assertEqual("pa1", denied.basis["preauth_id"])

        events = self.ledger.list_events(type="preauth_expired")
        self.assertEqual(1, len(events))
        self.assertEqual("pa1", events[0].entity_id)
        self.assertEqual(claim.claim_id, events[0].detail["claim_id"])
        self.assertTrue(events[0].at)  # 决定时点

    def test_valid_preauth_allows_reimbursement(self):
        self.ledger.add_preauthorization(
            preauth_id="pa1",
            policy_id="COMM-2026",
            course_id="c1",
            service_codes=["MRI-KNEE"],
            max_amount="3000.00",
            valid_from="2026-01-01",
            valid_until="2026-03-01",
        )
        receipt = post_receipt(
            self.ledger,
            lines=[{"service_code": "MRI-KNEE", "quantity": 1}],
            documents=["invoice_image"],
            service_date="2026-02-01",
        )
        claim = self.ledger.submit_claim(receipt.receipt_id)["claim"]
        # 商业 560 可报，俱乐部 (500+140)×0.5=320 可报，个人 320
        self.assertEqual("880.00", claim.totals["reimbursable"])
        self.assertEqual("320.00", claim.totals["personal"])
        self.assertEqual("0.00", claim.totals["denied"])
        self.assertEqual([], self.ledger.list_events(type="preauth_expired"))


class DuplicateTest(unittest.TestCase):
    def setUp(self):
        self.ledger = make_ledger()
        seed_world(self.ledger)

    def test_duplicate_claim_returns_original_with_status(self):
        receipt = post_receipt(
            self.ledger, lines=[{"service_code": "PT-KNEE-EXERCISE", "quantity": 1}]
        )
        first = self.ledger.submit_claim(receipt.receipt_id)
        self.assertFalse(first["duplicate"])
        again = self.ledger.submit_claim(receipt.receipt_id)
        self.assertTrue(again["duplicate"])
        self.assertEqual(first["claim"].claim_id, again["claim"].claim_id)
        self.assertEqual("approved", again["claim"].status)
        self.assertEqual(1, len(self.ledger.claims))

    def test_duplicate_receipt_returns_original_and_appends_image(self):
        first = post_receipt(
            self.ledger, lines=[{"service_code": "PT-KNEE-EXERCISE", "quantity": 1}]
        )
        result = self.ledger.add_receipt(
            player_id="p1",
            course_id="c1",
            vendor="康复中心",
            receipt_no="R-001",
            service_date="2026-01-12",
            lines=[{"service_code": "PT-KNEE-EXERCISE", "quantity": 1}],
            image={"image_ref": "img/resend-2.tif", "uploaded_by": "康复中心"},
        )
        self.assertTrue(result["duplicate"])
        self.assertEqual(first.receipt_id, result["receipt"].receipt_id)
        images = result["receipt"].images
        self.assertEqual(2, len(images))
        self.assertEqual("img/original-1.tif", images[0].image_ref)
        self.assertEqual("img/resend-2.tif", images[1].image_ref)
        self.assertEqual(1, len(self.ledger.receipts))


class PolicyYearTest(unittest.TestCase):
    def setUp(self):
        self.ledger = make_ledger()
        seed_world(self.ledger, effective_from="2025-01-01")
        self.ledger.add_policy(
            policy_id="COMM-2025",
            payer_type="commercial_insurance",
            payer_name="安联商业医疗",
            policy_year="2025",
            effective_from="2025-01-01",
            effective_to="2025-12-31",
            deductible="300.00",
            annual_limit="10000.00",
            rules=[{"service_code": "PT-KNEE-EXERCISE", "ratio": "0.8"}],
        )

    def test_policy_year_switch_recorded_and_deductible_resets(self):
        old = post_receipt(
            self.ledger,
            lines=[{"service_code": "PT-KNEE-EXERCISE", "quantity": 1}],
            service_date="2025-12-20",
            receipt_no="R-2025",
        )
        claim_old = self.ledger.submit_claim(old.receipt_id)["claim"]
        years = {p.basis.get("policy_year") for p in claim_old.portions if p.policy_id}
        self.assertIn("2025", years)

        new = post_receipt(
            self.ledger,
            lines=[{"service_code": "PT-KNEE-EXERCISE", "quantity": 1}],
            service_date="2026-01-12",
            receipt_no="R-2026",
        )
        self.ledger.submit_claim(new.receipt_id)

        events = self.ledger.list_events(type="policy_year_switch")
        self.assertEqual(1, len(events))
        self.assertEqual("2025", events[0].detail["from_policy_year"])
        self.assertEqual("2026", events[0].detail["to_policy_year"])
        self.assertTrue(events[0].at)

        summary = self.ledger.course_summary("p1", "c1")
        by_year = {p["policy_year"]: p for p in summary["policies"]}
        self.assertEqual("300.00", by_year["2025"]["deductible_used"])
        self.assertEqual("360.00", by_year["2026"]["deductible_used"])


class PlanChangeTest(unittest.TestCase):
    def setUp(self):
        self.ledger = make_ledger()
        seed_world(self.ledger)

    def test_plan_revision_records_decision_time(self):
        self.ledger.revise_prescription(
            "rx1",
            items=[
                {"service_code": "PT-KNEE-EXERCISE", "quantity": 20},
                {"service_code": "MRI-KNEE", "quantity": 2},
                {"service_code": "BRACE-KNEE", "quantity": 1},
            ],
            reason="术后增加支具",
            effective_from="2026-02-01",
            decided_at="2026-01-20T10:00:00+00:00",
        )
        events = self.ledger.list_events(type="plan_changed")
        self.assertEqual(1, len(events))
        self.assertEqual("2026-01-20T10:00:00+00:00", events[0].at)
        self.assertEqual(2, events[0].detail["revision"])

    def test_out_of_plan_service_is_denied(self):
        receipt = post_receipt(
            self.ledger,
            lines=[{"service_code": "HYDRO-THERAPY", "quantity": 1}],  # 不在处方内
        )
        claim = self.ledger.submit_claim(receipt.receipt_id)["claim"]
        self.assertEqual("denied", claim.status)
        self.assertEqual("520.00", claim.totals["denied"])
        portion = claim.portions[0]
        self.assertEqual("outside_treatment_plan", portion.reason)
        self.assertEqual(1, portion.basis["plan_revision"])

    def test_uncovered_planned_service_falls_to_personal(self):
        self.ledger.revise_prescription(
            "rx1",
            items=[
                {"service_code": "PT-KNEE-EXERCISE", "quantity": 20},
                {"service_code": "BRACE-KNEE", "quantity": 1},
            ],
            reason="增加支具",
            effective_from="2026-01-01",
        )
        receipt = post_receipt(
            self.ledger,
            lines=[{"service_code": "BRACE-KNEE", "quantity": 1}],
        )
        claim = self.ledger.submit_claim(receipt.receipt_id)["claim"]
        # 商业不保支具；俱乐部 "*" 规则承担一半，其余个人
        self.assertEqual("750.00", claim.totals["reimbursable"])
        self.assertEqual("750.00", claim.totals["personal"])


class PaymentBatchTest(unittest.TestCase):
    def setUp(self):
        self.ledger = make_ledger()
        seed_world(self.ledger)

    def test_batch_pays_and_trace_returns_liability_basis(self):
        receipt = post_receipt(
            self.ledger, lines=[{"service_code": "PT-KNEE-EXERCISE", "quantity": 10}]
        )
        claim = self.ledger.submit_claim(receipt.receipt_id)["claim"]

        batch = self.ledger.create_payment_batch("COMM-2026", created_by="会计-李")
        self.assertEqual("2480.00", batch.total)
        # 俱乐部部分未结算，理赔整体仍为 approved
        self.assertEqual("approved", self.ledger.get_claim(claim.claim_id).status)

        trace = self.ledger.batch_trace(batch.batch_id)
        self.assertEqual(1, len(trace["lines"]))
        line = trace["lines"][0]
        self.assertEqual("R-001", line["receipt_no"])
        self.assertEqual("康复中心", line["vendor"])
        portion = line["portions"][0]
        self.assertEqual("2480.00", portion["amount"])
        basis = portion["basis"]
        self.assertEqual("COMM-2026", basis["policy_id"])
        self.assertEqual("2026", basis["policy_year"])
        self.assertEqual("0.8", basis["ratio"])
        self.assertEqual("500.00", basis["deductible_applied"])
        self.assertTrue(any(e["type"] == "payout" for e in line["events"]))

        # 同一保单再次结算应无可付部分
        with self.assertRaises(ConflictError):
            self.ledger.create_payment_batch("COMM-2026")

        # 俱乐部部分单独结算后，理赔进入 paid
        batch2 = self.ledger.create_payment_batch("CLUB-2026")
        self.assertEqual("560.00", batch2.total)
        self.assertEqual("paid", self.ledger.get_claim(claim.claim_id).status)


class FollowUpTest(unittest.TestCase):
    def test_deadline_flags_follow_up_automatically(self):
        clock = FakeClock()
        ledger = make_ledger(clock=clock)
        seed_world(ledger)
        receipt = post_receipt(
            ledger,
            lines=[{"service_code": "PT-KNEE-EXERCISE", "quantity": 2}],
            documents=["invoice_image"],
        )
        claim = ledger.submit_claim(receipt.receipt_id)["claim"]
        self.assertEqual([], ledger.list_follow_ups())

        clock.advance(days=31)  # 超过 30 天补件期限
        follow_ups = ledger.list_follow_ups()
        self.assertEqual(1, len(follow_ups))
        self.assertEqual(claim.claim_id, follow_ups[0]["claim_id"])
        self.assertEqual(["itemized_bill"], follow_ups[0]["missing_documents"])
        self.assertEqual(1, len(ledger.list_events(type="follow_up_flagged")))

        # 补件后跟进标记解除
        ledger.submit_supplement(claim.claim_id, documents=["itemized_bill"])
        self.assertEqual([], ledger.list_follow_ups())

    def test_sweep_marks_at_given_time(self):
        clock = FakeClock()
        ledger = make_ledger(clock=clock)
        seed_world(ledger)
        receipt = post_receipt(
            ledger,
            lines=[{"service_code": "PT-KNEE-EXERCISE", "quantity": 2}],
            documents=["invoice_image"],
        )
        claim = ledger.submit_claim(receipt.receipt_id)["claim"]
        flagged = ledger.sweep(as_of="2026-03-01T00:00:00+00:00")
        self.assertEqual([claim.claim_id], flagged)


class ValidationTest(unittest.TestCase):
    def setUp(self):
        self.ledger = make_ledger()
        seed_world(self.ledger)

    def test_unknown_service_code_rejected(self):
        with self.assertRaises(ValidationError):
            post_receipt(self.ledger, lines=[{"service_code": "NOPE", "quantity": 1}])

    def test_missing_entities(self):
        with self.assertRaises(NotFoundError):
            self.ledger.submit_claim("rcpt_none")
        with self.assertRaises(NotFoundError):
            self.ledger.course_summary("p1", "c_none")

    def test_persistence_roundtrip(self):
        receipt = post_receipt(
            self.ledger, lines=[{"service_code": "PT-KNEE-EXERCISE", "quantity": 1}]
        )
        claim = self.ledger.submit_claim(receipt.receipt_id)["claim"]
        reloaded = Ledger(self.ledger.store, catalog=CATALOG)
        view = reloaded.get_claim(claim.claim_id)
        self.assertEqual(claim.totals, view.totals)
        self.assertEqual(len(claim.portions), len(view.portions))
        summary = reloaded.course_summary("p1", "c1")
        comm_view = [p for p in summary["policies"] if p["policy_id"] == "COMM-2026"][0]
        self.assertEqual("360.00", comm_view["deductible_used"])


if __name__ == "__main__":
    unittest.main()
