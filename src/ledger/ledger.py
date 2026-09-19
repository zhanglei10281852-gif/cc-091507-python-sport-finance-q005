"""理赔账本核心：费用拆分、支付方顺序、决定时点与审计事件。

拆分规则（金额守恒：可报 + 待补件 + 个人承担 + 已拒赔 = 费用总额）：
  1. 支付校方顺序由系统决定（商业保险 → 俱乐部福利 → 个人账户），前台无法手工指定；
  2. 每条费用先匹配治疗方案版本（按服务日期），计划外项目整条拒赔；
  3. 每张保单依次应用：免赔额部分流向下一支付方，责任比例部分归入该保单
     （可报 / 待补件 / 拒赔），自付与超限额部分继续流向下一支付方；
  4. 所有支付方处理完的余额归入个人承担。
"""
from __future__ import annotations

import threading
import uuid
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal

from .errors import ConflictError, NotFoundError, ValidationError
from .models import (
    CATEGORIES,
    CATEGORY_DENIED,
    CATEGORY_PENDING,
    CATEGORY_PERSONAL,
    CATEGORY_REIMBURSABLE,
    Claim,
    ClaimEffect,
    CoverageRule,
    Event,
    PaymentBatch,
    PaymentBatchLine,
    PlanItem,
    PlanRevision,
    Player,
    Policy,
    Portion,
    PreAuthorization,
    Prescription,
    Receipt,
    ReceiptImage,
    ReceiptLine,
    TreatmentCourse,
)
from .money import CENT, ZERO, money_str, parse_money, parse_quantity, parse_ratio, qty_str
from .store import Store
from .timeutil import now_utc, parse_date, parse_datetime, to_dt, to_iso

# 支付方顺序由系统决定，前台无需（也不能）手工指定
PAYER_PRIORITY = {
    "commercial_insurance": 10,
    "club_benefit": 20,
    "personal_account": 30,
}

# 事件类型（reference/domain.json 公开类型 + 决定时点事件）
EVT_PRESCRIPTION = "prescription"
EVT_PREAUTH = "pre_authorization"
EVT_RECEIPT = "receipt"
EVT_SUPPLEMENT = "supplement"
EVT_APPROVAL = "approval"
EVT_DENIAL = "denial"
EVT_PAYOUT = "payout"
EVT_PREAUTH_EXPIRED = "preauth_expired"
EVT_PLAN_CHANGED = "plan_changed"
EVT_POLICY_YEAR_SWITCH = "policy_year_switch"
EVT_FOLLOW_UP = "follow_up_flagged"

IMAGE_KINDS = ("original", "supplement")


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class Ledger:
    def __init__(
        self,
        store: Store,
        *,
        clock=None,
        catalog: dict | None = None,
        supplement_window_days: int = 30,
    ):
        self.store = store
        self.clock = clock or now_utc
        self.catalog = catalog or {}
        self.supplement_window_days = supplement_window_days
        self._lock = threading.RLock()
        raw = store.load()
        self.players = {d["player_id"]: Player.from_dict(d) for d in raw.get("players", [])}
        self.courses = {
            d["course_id"]: TreatmentCourse.from_dict(d) for d in raw.get("courses", [])
        }
        self.prescriptions = {
            d["prescription_id"]: Prescription.from_dict(d) for d in raw.get("prescriptions", [])
        }
        self.policies = {d["policy_id"]: Policy.from_dict(d) for d in raw.get("policies", [])}
        self.preauths = {
            d["preauth_id"]: PreAuthorization.from_dict(d)
            for d in raw.get("pre_authorizations", [])
        }
        self.receipts = {d["receipt_id"]: Receipt.from_dict(d) for d in raw.get("receipts", [])}
        self.claims = {d["claim_id"]: Claim.from_dict(d) for d in raw.get("claims", [])}
        self.batches = {
            d["batch_id"]: PaymentBatch.from_dict(d) for d in raw.get("payment_batches", [])
        }
        self.events = [Event.from_dict(d) for d in raw.get("events", [])]

    # ---------- 持久化 ----------

    def _dump(self) -> dict:
        return {
            "players": [p.to_dict() for p in self.players.values()],
            "courses": [c.to_dict() for c in self.courses.values()],
            "prescriptions": [p.to_dict() for p in self.prescriptions.values()],
            "policies": [p.to_dict() for p in self.policies.values()],
            "pre_authorizations": [p.to_dict() for p in self.preauths.values()],
            "receipts": [r.to_dict() for r in self.receipts.values()],
            "claims": [c.to_dict() for c in self.claims.values()],
            "payment_batches": [b.to_dict() for b in self.batches.values()],
            "events": [e.to_dict() for e in self.events],
        }

    def _save(self) -> None:
        self.store.save(self._dump())

    def _now(self) -> datetime:
        dt = self.clock()
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt

    def _now_iso(self) -> str:
        return to_iso(self._now())

    # ---------- 事件 ----------

    def _emit(self, type_, entity_kind, entity_id, detail, *, actor="system", at=None) -> Event:
        event = Event(
            event_id=_new_id("evt"),
            type=type_,
            at=at or self._now_iso(),
            actor=actor,
            entity_kind=entity_kind,
            entity_id=entity_id,
            detail=detail,
        )
        self.events.append(event)
        return event

    def _already_emitted(self, type_, entity_id, **detail_match) -> bool:
        """重算理赔时避免重复记录同一决定事件。"""
        for e in self.events:
            if e.type != type_ or e.entity_id != entity_id:
                continue
            if all(e.detail.get(k) == v for k, v in detail_match.items()):
                return True
        return False

    def list_events(self, *, type=None, entity_kind=None, entity_id=None) -> list[Event]:
        with self._lock:
            return [
                e
                for e in self.events
                if (type is None or e.type == type)
                and (entity_kind is None or e.entity_kind == entity_kind)
                and (entity_id is None or e.entity_id == entity_id)
            ]

    # ---------- 基础查询 ----------

    def _get_player(self, player_id: str) -> Player:
        player = self.players.get(player_id)
        if player is None:
            raise NotFoundError(f"球员不存在: {player_id}")
        return player

    def _get_course(self, course_id: str) -> TreatmentCourse:
        course = self.courses.get(course_id)
        if course is None:
            raise NotFoundError(f"疗程不存在: {course_id}")
        return course

    def _get_policy(self, policy_id: str) -> Policy:
        policy = self.policies.get(policy_id)
        if policy is None:
            raise NotFoundError(f"保单不存在: {policy_id}")
        return policy

    def _get_receipt(self, receipt_id: str) -> Receipt:
        receipt = self.receipts.get(receipt_id)
        if receipt is None:
            raise NotFoundError(f"收据不存在: {receipt_id}")
        return receipt

    def _get_claim(self, claim_id: str) -> Claim:
        claim = self.claims.get(claim_id)
        if claim is None:
            raise NotFoundError(f"理赔不存在: {claim_id}")
        return claim

    def _get_batch(self, batch_id: str) -> PaymentBatch:
        batch = self.batches.get(batch_id)
        if batch is None:
            raise NotFoundError(f"付款批次不存在: {batch_id}")
        return batch

    def get_player(self, player_id: str) -> Player:
        with self._lock:
            return self._get_player(player_id)

    def get_course(self, course_id: str) -> TreatmentCourse:
        with self._lock:
            return self._get_course(course_id)

    def get_receipt(self, receipt_id: str) -> Receipt:
        with self._lock:
            return self._get_receipt(receipt_id)

    def get_claim(self, claim_id: str) -> Claim:
        with self._lock:
            self._sweep_locked(self._now())
            return self._get_claim(claim_id)

    def get_batch(self, batch_id: str) -> PaymentBatch:
        with self._lock:
            return self._get_batch(batch_id)

    def list_policies(self) -> list[Policy]:
        with self._lock:
            return sorted(self.policies.values(), key=lambda p: (p.priority, p.policy_id))

    def list_claims(self, *, player_id=None, course_id=None, status=None) -> list[Claim]:
        with self._lock:
            self._sweep_locked(self._now())
            return [
                c
                for c in self.claims.values()
                if (player_id is None or c.player_id == player_id)
                and (course_id is None or c.course_id == course_id)
                and (status is None or c.status == status)
            ]

    # ---------- 目录 ----------

    def _catalog_item(self, service_code: str) -> dict | None:
        for item in self.catalog.get("service_items", []):
            if item.get("code") == service_code:
                return item
        return None

    def _check_service_code(self, service_code: str) -> None:
        if self.catalog.get("service_items") and self._catalog_item(service_code) is None:
            raise ValidationError(
                f"未知服务项目: {service_code}", details={"service_code": service_code}
            )

    # ---------- 球员 / 疗程 / 处方 ----------

    def add_player(self, *, name, team="", player_id=None) -> Player:
        with self._lock:
            if not name:
                raise ValidationError("name 不能为空")
            pid = player_id or _new_id("plr")
            if pid in self.players:
                raise ConflictError(f"球员已存在: {pid}")
            player = Player(player_id=pid, name=name, team=team, created_at=self._now_iso())
            self.players[pid] = player
            self._save()
            return player

    def add_course(self, *, player_id, diagnosis, started_at, course_id=None) -> TreatmentCourse:
        with self._lock:
            self._get_player(player_id)
            cid = course_id or _new_id("crs")
            if cid in self.courses:
                raise ConflictError(f"疗程已存在: {cid}")
            course = TreatmentCourse(
                course_id=cid,
                player_id=player_id,
                diagnosis=diagnosis or "",
                started_at=parse_date(started_at, "started_at"),
                status="active",
                created_at=self._now_iso(),
            )
            self.courses[cid] = course
            self._save()
            return course

    def _parse_plan_items(self, items) -> list[PlanItem]:
        if not isinstance(items, list) or not items:
            raise ValidationError("处方至少需要一项治疗项目")
        seen = set()
        plan = []
        for i, raw in enumerate(items):
            code = (raw or {}).get("service_code")
            if not code:
                raise ValidationError(f"items[{i}] 缺少 service_code")
            if code in seen:
                raise ValidationError(f"治疗项目重复: {code}")
            self._check_service_code(code)
            qty = parse_quantity(raw.get("quantity", 1), f"items[{i}].quantity")
            if qty <= 0:
                raise ValidationError(f"items[{i}].quantity 必须大于 0")
            seen.add(code)
            plan.append(PlanItem(service_code=code, quantity=qty_str(qty)))
        return plan

    def add_prescription(
        self,
        *,
        course_id,
        items,
        effective_from,
        decided_at=None,
        reason="初始处方",
        prescription_id=None,
        actor="team_doctor",
    ) -> Prescription:
        with self._lock:
            course = self._get_course(course_id)
            eff = parse_date(effective_from, "effective_from")
            decided = parse_datetime(decided_at, "decided_at") if decided_at else self._now_iso()
            plan_items = self._parse_plan_items(items)
            pid = prescription_id or _new_id("prsc")
            if pid in self.prescriptions:
                raise ConflictError(f"处方已存在: {pid}")
            rx = Prescription(
                prescription_id=pid,
                course_id=course_id,
                player_id=course.player_id,
                revisions=[
                    PlanRevision(
                        revision=1,
                        items=plan_items,
                        reason=reason,
                        effective_from=eff,
                        decided_at=decided,
                    )
                ],
                created_at=self._now_iso(),
            )
            self.prescriptions[pid] = rx
            self._emit(
                EVT_PRESCRIPTION,
                "prescription",
                pid,
                {"course_id": course_id, "revision": 1, "reason": reason},
                actor=actor,
                at=decided,
            )
            self._save()
            return rx

    def revise_prescription(
        self, prescription_id, *, items, reason, effective_from, decided_at=None, actor="team_doctor"
    ) -> Prescription:
        """治疗方案变更：追加新版本并留下决定时点（plan_changed 事件）。"""
        with self._lock:
            rx = self.prescriptions.get(prescription_id)
            if rx is None:
                raise NotFoundError(f"处方不存在: {prescription_id}")
            eff = parse_date(effective_from, "effective_from")
            decided = parse_datetime(decided_at, "decided_at") if decided_at else self._now_iso()
            plan_items = self._parse_plan_items(items)
            revision = PlanRevision(
                revision=len(rx.revisions) + 1,
                items=plan_items,
                reason=reason or "",
                effective_from=eff,
                decided_at=decided,
            )
            rx.revisions.append(revision)
            self._emit(
                EVT_PLAN_CHANGED,
                "prescription",
                prescription_id,
                {
                    "course_id": rx.course_id,
                    "revision": revision.revision,
                    "reason": revision.reason,
                    "effective_from": eff,
                },
                actor=actor,
                at=decided,
            )
            self._save()
            return rx

    # ---------- 保单 / 预授权 ----------

    def add_policy(
        self,
        *,
        payer_type,
        payer_name,
        policy_year,
        effective_from,
        effective_to,
        deductible="0",
        annual_limit="0",
        currency="CNY",
        priority=None,
        rules=None,
        policy_id=None,
    ) -> Policy:
        with self._lock:
            if payer_type not in PAYER_PRIORITY:
                raise ValidationError(f"未知支付方类型: {payer_type}")
            eff_from = parse_date(effective_from, "effective_from")
            eff_to = parse_date(effective_to, "effective_to")
            if eff_from > eff_to:
                raise ValidationError("effective_from 不能晚于 effective_to")
            ded = parse_money(deductible, "deductible")
            lim = parse_money(annual_limit, "annual_limit")
            if ded < ZERO or lim < ZERO:
                raise ValidationError("免赔额与年度额度不能为负")
            parsed_rules = []
            for i, raw in enumerate(rules or []):
                code = (raw or {}).get("service_code")
                if not code:
                    raise ValidationError(f"rules[{i}] 缺少 service_code")
                ratio = parse_ratio(raw.get("ratio"), f"rules[{i}].ratio")
                parsed_rules.append(
                    CoverageRule(
                        service_code=code,
                        ratio=str(ratio),
                        requires_preauth=bool(raw.get("requires_preauth", False)),
                        required_documents=list(raw.get("required_documents") or []),
                    )
                )
            pid = policy_id or _new_id("pol")
            if pid in self.policies:
                raise ConflictError(f"保单已存在: {pid}")
            policy = Policy(
                policy_id=pid,
                payer_type=payer_type,
                payer_name=payer_name or "",
                policy_year=str(policy_year),
                currency=currency or "CNY",
                effective_from=eff_from,
                effective_to=eff_to,
                deductible=money_str(ded),
                annual_limit=money_str(lim),
                priority=priority if priority is not None else PAYER_PRIORITY[payer_type],
                rules=parsed_rules,
                created_at=self._now_iso(),
            )
            self.policies[pid] = policy
            self._save()
            return policy

    def add_preauthorization(
        self,
        *,
        policy_id,
        course_id,
        max_amount,
        valid_from,
        valid_until,
        service_codes=None,
        preauth_id=None,
        actor="insurer",
    ) -> PreAuthorization:
        with self._lock:
            policy = self._get_policy(policy_id)
            course = self._get_course(course_id)
            vf = parse_date(valid_from, "valid_from")
            vu = parse_date(valid_until, "valid_until")
            if vf > vu:
                raise ValidationError("valid_from 不能晚于 valid_until")
            amount = parse_money(max_amount, "max_amount")
            if amount <= ZERO:
                raise ValidationError("max_amount 必须大于 0")
            codes = list(service_codes or [])
            for code in codes:
                self._check_service_code(code)
            pid = preauth_id or _new_id("pauth")
            if pid in self.preauths:
                raise ConflictError(f"预授权已存在: {pid}")
            preauth = PreAuthorization(
                preauth_id=pid,
                policy_id=policy_id,
                course_id=course_id,
                player_id=course.player_id,
                service_codes=codes,
                max_amount=money_str(amount),
                valid_from=vf,
                valid_until=vu,
                status="active",
                created_at=self._now_iso(),
            )
            self.preauths[pid] = preauth
            self._emit(
                EVT_PREAUTH,
                "pre_authorization",
                pid,
                {"policy_id": policy_id, "course_id": course_id, "valid_until": vu},
                actor=actor,
            )
            self._save()
            return preauth

    # ---------- 收据 ----------

    def _build_lines(self, lines) -> list[ReceiptLine]:
        if not isinstance(lines, list) or not lines:
            raise ValidationError("收据至少需要一条费用行")
        built = []
        for i, raw in enumerate(lines, 1):
            code = (raw or {}).get("service_code")
            if not code:
                raise ValidationError(f"lines[{i}] 缺少 service_code")
            item = self._catalog_item(code)
            self._check_service_code(code)
            qty = parse_quantity(raw.get("quantity"), f"lines[{i}].quantity")
            if qty <= 0:
                raise ValidationError(f"lines[{i}].quantity 必须大于 0")
            unit = raw.get("unit_price")
            if unit is None and item is not None:
                unit = item.get("unit_price")
            if unit is None:
                raise ValidationError(f"lines[{i}] 缺少 unit_price: {code}")
            unit = parse_money(unit, f"lines[{i}].unit_price")
            amount = (qty * unit).quantize(CENT, rounding=ROUND_HALF_UP)
            built.append(
                ReceiptLine(
                    line_id=f"ln_{i}",
                    service_code=code,
                    description=raw.get("description") or (item.get("name") if item else ""),
                    quantity=qty_str(qty),
                    unit_price=money_str(unit),
                    amount=money_str(amount),
                )
            )
        return built

    def _append_image(
        self, receipt: Receipt, *, image_ref, uploaded_by, kind=None, note="", at=None
    ) -> ReceiptImage:
        """追加影像版本。版本号单调递增，原始影像永不被覆盖。"""
        if not image_ref:
            raise ValidationError("image_ref 不能为空")
        version = len(receipt.images) + 1
        resolved_kind = kind or ("original" if version == 1 else "supplement")
        if resolved_kind not in IMAGE_KINDS:
            raise ValidationError(f"未知影像类型: {resolved_kind}")
        image = ReceiptImage(
            version=version,
            kind=resolved_kind,
            image_ref=image_ref,
            uploaded_by=uploaded_by or "vendor",
            uploaded_at=at or self._now_iso(),
            note=note or "",
        )
        receipt.images.append(image)
        return image

    def add_receipt(
        self,
        *,
        player_id,
        course_id,
        vendor,
        receipt_no,
        service_date,
        lines,
        currency="CNY",
        documents=None,
        image=None,
        received_at=None,
        receipt_id=None,
        uploaded_by="vendor",
    ) -> dict:
        """登记收据。同一球员 + 供应商 + 票号重复提交时返回原收据（幂等），
        供应商补传的影像只追加为新版本，不覆盖原始影像。"""
        with self._lock:
            self._get_player(player_id)
            course = self._get_course(course_id)
            if course.player_id != player_id:
                raise ValidationError(
                    "疗程不属于该球员",
                    details={"course_id": course_id, "player_id": player_id},
                )
            receipt_key = f"{player_id}::{vendor}::{receipt_no}"
            for existing in self.receipts.values():
                if existing.receipt_key != receipt_key:
                    continue
                merged = False
                for doc in documents or []:
                    if doc not in existing.documents:
                        existing.documents.append(doc)
                        merged = True
                if image:
                    self._append_image(
                        existing,
                        image_ref=image.get("image_ref"),
                        uploaded_by=image.get("uploaded_by", uploaded_by),
                        kind=image.get("kind"),
                        note=image.get("note", ""),
                    )
                    merged = True
                if merged:
                    self._save()
                return {"receipt": existing, "duplicate": True, "merged": merged}

            rid = receipt_id or _new_id("rcpt")
            if rid in self.receipts:
                raise ConflictError(f"收据已存在: {rid}")
            receipt = Receipt(
                receipt_id=rid,
                receipt_key=receipt_key,
                player_id=player_id,
                course_id=course_id,
                vendor=vendor,
                receipt_no=receipt_no,
                currency=currency or "CNY",
                service_date=parse_date(service_date, "service_date"),
                received_at=(
                    parse_datetime(received_at, "received_at") if received_at else self._now_iso()
                ),
                lines=self._build_lines(lines),
                documents=list(documents or []),
                images=[],
                created_at=self._now_iso(),
            )
            if image:
                self._append_image(
                    receipt,
                    image_ref=image.get("image_ref"),
                    uploaded_by=image.get("uploaded_by", uploaded_by),
                    kind=image.get("kind") or "original",
                    note=image.get("note", ""),
                )
            self.receipts[rid] = receipt
            self._emit(
                EVT_RECEIPT,
                "receipt",
                rid,
                {"receipt_no": receipt_no, "vendor": vendor, "service_date": receipt.service_date},
                actor=uploaded_by,
            )
            self._save()
            return {"receipt": receipt, "duplicate": False, "merged": False}

    def add_receipt_image(
        self, receipt_id, *, image_ref, uploaded_by="vendor", kind=None, note="", uploaded_at=None
    ) -> Receipt:
        with self._lock:
            receipt = self._get_receipt(receipt_id)
            at = parse_datetime(uploaded_at, "uploaded_at") if uploaded_at else None
            image = self._append_image(
                receipt,
                image_ref=image_ref,
                uploaded_by=uploaded_by,
                kind=kind,
                note=note,
                at=at,
            )
            if image.kind == "supplement":
                self._emit(
                    EVT_SUPPLEMENT,
                    "receipt",
                    receipt_id,
                    {"image_version": image.version, "image_ref": image.image_ref},
                    actor=uploaded_by,
                    at=image.uploaded_at,
                )
            self._save()
            return receipt

    # ---------- 理赔拆分 ----------

    def _prescription_for_course(self, course_id: str) -> Prescription | None:
        candidates = [rx for rx in self.prescriptions.values() if rx.course_id == course_id]
        if not candidates:
            return None
        return max(candidates, key=lambda rx: rx.created_at)

    def _policies_for(self, receipt: Receipt) -> list[Policy]:
        """按系统顺序返回服务日期当日生效的保单（个人账户为兜底，不在此列）。"""
        pols = [
            p
            for p in self.policies.values()
            if p.payer_type != "personal_account"
            and p.currency == receipt.currency
            and p.effective_from <= receipt.service_date <= p.effective_to
        ]
        pols.sort(key=lambda p: (p.priority, p.policy_id))
        return pols

    def _accumulator(self, player_id, policy_id, *, exclude_claim=None, extra=None):
        """球员在某保单下已累计的免赔额与额度占用（排除正在重算的理赔）。"""
        ded = ZERO
        lim = ZERO
        for claim in self.claims.values():
            if claim.player_id != player_id or claim.claim_id == exclude_claim:
                continue
            for eff in claim.effects:
                if eff.policy_id == policy_id:
                    ded += Decimal(eff.deductible_applied)
                    lim += Decimal(eff.limit_reserved)
        for pid, eff in (extra or {}).items():
            if pid == policy_id:
                ded += eff["ded"]
                lim += eff["lim"]
        return ded, lim

    def _preauth_usage(self, preauth_id, *, exclude_claim=None, extra_portions=None) -> Decimal:
        total = ZERO
        claims = [c for c in self.claims.values() if c.claim_id != exclude_claim]
        for claim in claims:
            for p in claim.portions:
                if p.basis.get("preauth_id") == preauth_id and p.category in (
                    CATEGORY_REIMBURSABLE,
                    CATEGORY_PENDING,
                ):
                    total += Decimal(p.amount)
        for p in extra_portions or []:
            if p.basis.get("preauth_id") == preauth_id and p.category in (
                CATEGORY_REIMBURSABLE,
                CATEGORY_PENDING,
            ):
                total += Decimal(p.amount)
        return total

    def _preauth_candidates(self, policy_id, course_id, service_code):
        return [
            pa
            for pa in self.preauths.values()
            if pa.policy_id == policy_id
            and pa.course_id == course_id
            and pa.status == "active"
            and (not pa.service_codes or service_code in pa.service_codes)
        ]

    def _find_preauth(self, policy_id, course_id, service_code, service_date):
        valid = [
            pa
            for pa in self._preauth_candidates(policy_id, course_id, service_code)
            if pa.valid_from <= service_date <= pa.valid_until
        ]
        if not valid:
            return None
        return max(valid, key=lambda pa: pa.valid_until)

    def _find_expired_preauth(self, policy_id, course_id, service_code, service_date):
        expired = [
            pa
            for pa in self._preauth_candidates(policy_id, course_id, service_code)
            if pa.valid_until < service_date
        ]
        if not expired:
            return None
        return max(expired, key=lambda pa: pa.valid_until)

    def _last_policy_year(self, player_id, course_id, payer_type, *, exclude_claim):
        """该疗程此前在同类型支付方下使用的最近保单年度，用于识别跨年度切换。"""
        best = None
        for claim in self.claims.values():
            if (
                claim.player_id != player_id
                or claim.course_id != course_id
                or claim.claim_id == exclude_claim
            ):
                continue
            for p in claim.portions:
                if p.payer_type != payer_type or not p.policy_id:
                    continue
                year = p.basis.get("policy_year")
                if year and (best is None or claim.submitted_at > best[0]):
                    best = (claim.submitted_at, year)
        return best[1] if best else None

    def submit_claim(self, receipt_id, *, submitted_by="frontdesk", submitted_at=None) -> dict:
        """提交理赔并拆分。同一收据重复提交时返回原申请及其当前状态。"""
        with self._lock:
            receipt = self._get_receipt(receipt_id)
            for claim in self.claims.values():
                if claim.receipt_id == receipt_id:
                    self._sweep_locked(self._now())
                    return {"claim": claim, "duplicate": True}
            now = self._now()
            submitted = (
                parse_datetime(submitted_at, "submitted_at") if submitted_at else to_iso(now)
            )
            claim = Claim(
                claim_id=_new_id("clm"),
                receipt_id=receipt_id,
                player_id=receipt.player_id,
                course_id=receipt.course_id,
                status="submitted",
                portions=[],
                effects=[],
                totals={c: "0.00" for c in CATEGORIES},
                supplement_deadline=None,
                follow_up=False,
                follow_up_at=None,
                submitted_by=submitted_by,
                submitted_at=submitted,
                decided_at=submitted,
                paid_at=None,
                history=[{"at": submitted, "from": None, "to": "submitted", "by": submitted_by}],
            )
            self._adjudicate_locked(claim, receipt, now)
            self.claims[claim.claim_id] = claim
            self._save()
            return {"claim": claim, "duplicate": False}

    def _adjudicate_locked(self, claim: Claim, receipt: Receipt, now: datetime) -> None:
        """按支付方顺序拆分整张收据，重写理赔的归属、累计器影响与状态。"""
        now_iso = to_iso(now)
        rx = self._prescription_for_course(claim.course_id)
        policies = self._policies_for(receipt)
        portions: list[Portion] = []
        effects: dict[str, dict] = {}
        switch_checked: set[str] = set()

        for line in receipt.lines:
            remaining = Decimal(line.amount)
            revision = rx.revision_at(receipt.service_date) if rx else None
            revision_no = revision.revision if revision else None

            # 治疗方案外项目：整条拒赔
            if rx is not None:
                in_plan = revision is not None and any(
                    i.service_code == line.service_code for i in revision.items
                )
                if not in_plan:
                    portions.append(
                        Portion(
                            portion_id=_new_id("por"),
                            line_id=line.line_id,
                            service_code=line.service_code,
                            payer_type=None,
                            policy_id=None,
                            category=CATEGORY_DENIED,
                            amount=line.amount,
                            reason="outside_treatment_plan",
                            status="denied",
                            basis={
                                "service_code": line.service_code,
                                "service_date": receipt.service_date,
                                "prescription_id": rx.prescription_id,
                                "plan_revision": revision_no,
                                "decision": "denied",
                            },
                        )
                    )
                    continue

            for policy in policies:
                if remaining <= ZERO:
                    break
                rule = policy.match(line.service_code)
                if rule is None:
                    continue

                # 跨年度保单切换：留下决定时点
                if policy.payer_type not in switch_checked:
                    switch_checked.add(policy.payer_type)
                    last_year = self._last_policy_year(
                        claim.player_id,
                        claim.course_id,
                        policy.payer_type,
                        exclude_claim=claim.claim_id,
                    )
                    if (
                        last_year
                        and last_year != policy.policy_year
                        and not self._already_emitted(
                            EVT_POLICY_YEAR_SWITCH,
                            claim.claim_id,
                            payer_type=policy.payer_type,
                        )
                    ):
                        self._emit(
                            EVT_POLICY_YEAR_SWITCH,
                            "claim",
                            claim.claim_id,
                            {
                                "player_id": claim.player_id,
                                "course_id": claim.course_id,
                                "payer_type": policy.payer_type,
                                "from_policy_year": last_year,
                                "to_policy_year": policy.policy_year,
                                "to_policy_id": policy.policy_id,
                                "service_date": receipt.service_date,
                            },
                            actor="system",
                            at=now_iso,
                        )

                # 预授权
                preauth = None
                preauth_problem = None
                if rule.requires_preauth:
                    preauth = self._find_preauth(
                        policy.policy_id, claim.course_id, line.service_code, receipt.service_date
                    )
                    if preauth is None:
                        expired = self._find_expired_preauth(
                            policy.policy_id,
                            claim.course_id,
                            line.service_code,
                            receipt.service_date,
                        )
                        if expired is not None:
                            preauth = expired
                            preauth_problem = "preauth_expired"
                            if not self._already_emitted(
                                EVT_PREAUTH_EXPIRED,
                                expired.preauth_id,
                                claim_id=claim.claim_id,
                            ):
                                self._emit(
                                    EVT_PREAUTH_EXPIRED,
                                    "pre_authorization",
                                    expired.preauth_id,
                                    {
                                        "claim_id": claim.claim_id,
                                        "receipt_id": receipt.receipt_id,
                                        "policy_id": policy.policy_id,
                                        "service_date": receipt.service_date,
                                        "valid_until": expired.valid_until,
                                    },
                                    actor="system",
                                    at=now_iso,
                                )
                        else:
                            preauth_problem = "preauth_missing"

                # 免赔额与年度额度（排除本理赔旧影响，计入本次已算部分）
                ded_used, lim_used = self._accumulator(
                    claim.player_id, policy.policy_id, exclude_claim=claim.claim_id, extra=effects
                )
                ded_room = max(ZERO, Decimal(policy.deductible) - ded_used)
                ded_apply = min(remaining, ded_room)
                after_ded = remaining - ded_apply
                share = (after_ded * Decimal(rule.ratio)).quantize(
                    CENT, rounding=ROUND_HALF_UP
                )
                coshare = after_ded - share
                limit_room = max(ZERO, Decimal(policy.annual_limit) - lim_used)
                payable = min(share, limit_room)
                excess = share - payable

                # 预授权额度
                if preauth is not None and preauth_problem is None:
                    used = self._preauth_usage(
                        preauth.preauth_id, exclude_claim=claim.claim_id, extra_portions=portions
                    )
                    pa_room = max(ZERO, Decimal(preauth.max_amount) - used)
                    pa_excess = max(ZERO, payable - pa_room)
                    payable -= pa_excess
                    excess += pa_excess

                # 决定该保单承担部分的归属
                missing = [d for d in rule.required_documents if d not in receipt.documents]
                if preauth_problem:
                    category, reason, pstatus = CATEGORY_DENIED, preauth_problem, "denied"
                elif missing:
                    category, reason, pstatus = CATEGORY_PENDING, "missing_documents", "pending"
                else:
                    category, reason, pstatus = CATEGORY_REIMBURSABLE, "covered", "approved"

                basis = {
                    "service_code": line.service_code,
                    "service_date": receipt.service_date,
                    "policy_id": policy.policy_id,
                    "policy_year": policy.policy_year,
                    "payer_type": policy.payer_type,
                    "ratio": rule.ratio,
                    "rule_service_code": rule.service_code,
                    "plan_revision": revision_no,
                    "deductible_applied": money_str(ded_apply),
                    "annual_limit_remaining_before": money_str(limit_room),
                    "preauth_id": preauth.preauth_id if preauth else None,
                }
                if missing:
                    basis["missing_documents"] = missing

                if payable > ZERO:
                    portions.append(
                        Portion(
                            portion_id=_new_id("por"),
                            line_id=line.line_id,
                            service_code=line.service_code,
                            payer_type=policy.payer_type,
                            policy_id=policy.policy_id,
                            category=category,
                            amount=money_str(payable),
                            reason=reason,
                            status=pstatus,
                            basis=basis,
                        )
                    )
                eff = effects.setdefault(
                    policy.policy_id, {"policy_year": policy.policy_year, "ded": ZERO, "lim": ZERO}
                )
                eff["ded"] += ded_apply
                eff["lim"] += payable
                # 免赔额、自付与超限额部分流向下一支付方
                remaining = ded_apply + coshare + excess

            # 所有支付方处理完的余额由个人承担
            if remaining > ZERO:
                portions.append(
                    Portion(
                        portion_id=_new_id("por"),
                        line_id=line.line_id,
                        service_code=line.service_code,
                        payer_type="personal_account",
                        policy_id=None,
                        category=CATEGORY_PERSONAL,
                        amount=money_str(remaining),
                        reason="residual_after_payers",
                        status="personal",
                        basis={
                            "service_code": line.service_code,
                            "service_date": receipt.service_date,
                            "plan_revision": revision_no,
                            "payers_applied": [
                                p.policy_id for p in policies if p.match(line.service_code)
                            ],
                        },
                    )
                )

        claim.portions = portions
        claim.effects = [
            ClaimEffect(
                policy_id=pid,
                policy_year=eff["policy_year"],
                deductible_applied=money_str(eff["ded"]),
                limit_reserved=money_str(eff["lim"]),
            )
            for pid, eff in effects.items()
            if eff["ded"] > ZERO or eff["lim"] > ZERO
        ]
        totals = {c: ZERO for c in CATEGORIES}
        for p in portions:
            totals[p.category] += Decimal(p.amount)
        claim.totals = {k: money_str(v) for k, v in totals.items()}

        old_status = claim.status
        if totals[CATEGORY_PENDING] > ZERO:
            claim.status = "needs_documents"
            if claim.supplement_deadline is None:
                claim.supplement_deadline = to_iso(
                    now + timedelta(days=self.supplement_window_days)
                )
        elif totals[CATEGORY_REIMBURSABLE] > ZERO:
            claim.status = "approved"
            claim.supplement_deadline = None
        elif totals[CATEGORY_DENIED] > ZERO:
            claim.status = "denied"
            claim.supplement_deadline = None
        else:
            # 全部个人承担：无可赔付部分，视为核定完成
            claim.status = "approved"
            claim.supplement_deadline = None
        if claim.status != "needs_documents":
            claim.follow_up = False
            claim.follow_up_at = None
        claim.decided_at = now_iso
        claim.history.append(
            {"at": now_iso, "from": old_status, "to": claim.status, "by": "adjudication"}
        )
        if claim.status == "approved":
            self._emit(
                EVT_APPROVAL,
                "claim",
                claim.claim_id,
                {"receipt_id": receipt.receipt_id, "reimbursable": claim.totals[CATEGORY_REIMBURSABLE]},
                actor="system",
                at=now_iso,
            )
        elif claim.status == "denied":
            self._emit(
                EVT_DENIAL,
                "claim",
                claim.claim_id,
                {"receipt_id": receipt.receipt_id, "denied": claim.totals[CATEGORY_DENIED]},
                actor="system",
                at=now_iso,
            )

    def submit_supplement(
        self, claim_id, *, documents=None, image=None, uploaded_by="provider", at=None
    ) -> Claim:
        """补件：合并单据、追加影像版本（不覆盖原始影像），随后重算拆分。"""
        with self._lock:
            claim = self._get_claim(claim_id)
            if claim.status != "needs_documents":
                raise ConflictError(
                    "仅待补件状态的理赔可以补件",
                    details={"claim_id": claim_id, "status": claim.status},
                )
            receipt = self.receipts[claim.receipt_id]
            now = to_dt(parse_datetime(at, "at")) if at else self._now()
            now_iso = to_iso(now)
            for doc in documents or []:
                if doc not in receipt.documents:
                    receipt.documents.append(doc)
            if image:
                self._append_image(
                    receipt,
                    image_ref=image.get("image_ref"),
                    uploaded_by=image.get("uploaded_by", uploaded_by),
                    kind=image.get("kind") or "supplement",
                    note=image.get("note", ""),
                    at=now_iso,
                )
            self._emit(
                EVT_SUPPLEMENT,
                "claim",
                claim.claim_id,
                {
                    "receipt_id": receipt.receipt_id,
                    "documents": list(documents or []),
                    "image_added": bool(image),
                },
                actor=uploaded_by,
                at=now_iso,
            )
            claim.history.append({"at": now_iso, "event": "supplement_received", "by": uploaded_by})
            self._adjudicate_locked(claim, receipt, now)
            self._save()
            return claim

    # ---------- 付款批次 ----------

    def create_payment_batch(self, policy_id, *, claim_ids=None, created_by="accounting") -> PaymentBatch:
        with self._lock:
            policy = self._get_policy(policy_id)
            now_iso = self._now_iso()
            lines = []
            touched = []
            for claim in self.claims.values():
                if claim.status != "approved":
                    continue
                if claim_ids is not None and claim.claim_id not in claim_ids:
                    continue
                payable = [
                    p
                    for p in claim.portions
                    if p.category == CATEGORY_REIMBURSABLE
                    and p.policy_id == policy_id
                    and p.status == "approved"
                ]
                if not payable:
                    continue
                amount = sum((Decimal(p.amount) for p in payable), ZERO)
                lines.append(
                    PaymentBatchLine(
                        claim_id=claim.claim_id,
                        receipt_id=claim.receipt_id,
                        portion_ids=[p.portion_id for p in payable],
                        amount=money_str(amount),
                    )
                )
                touched.append((claim, payable))
            if not lines:
                raise ConflictError(
                    "没有可结算的理赔部分", details={"policy_id": policy_id}
                )
            batch_id = _new_id("batch")
            for claim, payable in touched:
                for p in payable:
                    p.status = "paid"
                    p.paid_batch_id = batch_id
                if all(
                    p.category != CATEGORY_REIMBURSABLE or p.status == "paid"
                    for p in claim.portions
                ):
                    claim.status = "paid"
                    claim.paid_at = now_iso
                    claim.history.append(
                        {"at": now_iso, "from": "approved", "to": "paid", "by": created_by}
                    )
                amount = sum((Decimal(p.amount) for p in payable), ZERO)
                self._emit(
                    EVT_PAYOUT,
                    "claim",
                    claim.claim_id,
                    {"batch_id": batch_id, "policy_id": policy_id, "amount": money_str(amount)},
                    actor=created_by,
                    at=now_iso,
                )
            total = sum((Decimal(line.amount) for line in lines), ZERO)
            batch = PaymentBatch(
                batch_id=batch_id,
                policy_id=policy_id,
                payer_type=policy.payer_type,
                currency=policy.currency,
                lines=lines,
                total=money_str(total),
                created_by=created_by,
                created_at=now_iso,
            )
            self.batches[batch_id] = batch
            self._save()
            return batch

    def batch_trace(self, batch_id) -> dict:
        """从付款批次反查每张收据的责任依据（portion.basis）与决定事件。"""
        with self._lock:
            batch = self._get_batch(batch_id)
            lines = []
            for line in batch.lines:
                claim = self.claims[line.claim_id]
                receipt = self.receipts[claim.receipt_id]
                paid_portions = [
                    p for p in claim.portions if p.portion_id in set(line.portion_ids)
                ]
                events = [
                    e
                    for e in self.events
                    if e.entity_kind == "claim" and e.entity_id == claim.claim_id
                ]
                lines.append(
                    {
                        "claim_id": claim.claim_id,
                        "claim_status": claim.status,
                        "receipt_id": receipt.receipt_id,
                        "receipt_no": receipt.receipt_no,
                        "vendor": receipt.vendor,
                        "service_date": receipt.service_date,
                        "amount": line.amount,
                        "portions": [p.to_dict() for p in paid_portions],
                        "receipt_images": [img.to_dict() for img in receipt.images],
                        "events": [e.to_dict() for e in events],
                    }
                )
            return {"batch": batch.to_dict(), "lines": lines}

    # ---------- 队医视图 ----------

    def course_summary(self, player_id, course_id) -> dict:
        """按球员和疗程汇总：累计免赔额、剩余额度与费用归属。"""
        with self._lock:
            player = self._get_player(player_id)
            course = self._get_course(course_id)
            if course.player_id != player_id:
                raise ValidationError(
                    "疗程不属于该球员",
                    details={"course_id": course_id, "player_id": player_id},
                )
            self._sweep_locked(self._now())
            claims = [c for c in self.claims.values() if c.course_id == course_id]
            totals = {c: ZERO for c in CATEGORIES}
            for claim in claims:
                for key in totals:
                    totals[key] += Decimal(claim.totals[key])

            # 免赔额与额度按球员 + 保单年度累计（可跨疗程）
            acc: dict[str, list] = {}
            for claim in self.claims.values():
                if claim.player_id != player_id:
                    continue
                for eff in claim.effects:
                    slot = acc.setdefault(eff.policy_id, [ZERO, ZERO, eff.policy_year])
                    slot[0] += Decimal(eff.deductible_applied)
                    slot[1] += Decimal(eff.limit_reserved)
            policies_view = []
            for pid, (ded, lim, _year) in acc.items():
                policy = self.policies.get(pid)
                if policy is None:
                    continue
                policies_view.append(
                    {
                        "policy_id": pid,
                        "payer_type": policy.payer_type,
                        "payer_name": policy.payer_name,
                        "policy_year": policy.policy_year,
                        "deductible": policy.deductible,
                        "deductible_used": money_str(ded),
                        "deductible_remaining": money_str(
                            max(ZERO, Decimal(policy.deductible) - ded)
                        ),
                        "annual_limit": policy.annual_limit,
                        "limit_used": money_str(lim),
                        "limit_remaining": money_str(
                            max(ZERO, Decimal(policy.annual_limit) - lim)
                        ),
                    }
                )
            policies_view.sort(key=lambda p: (p["policy_year"], p["payer_type"]))

            return {
                "player_id": player_id,
                "player_name": player.name,
                "course_id": course_id,
                "diagnosis": course.diagnosis,
                "course_status": course.status,
                "totals": {k: money_str(v) for k, v in totals.items()},
                "policies": policies_view,
                "claims": [
                    {
                        "claim_id": c.claim_id,
                        "receipt_id": c.receipt_id,
                        "status": c.status,
                        "totals": dict(c.totals),
                        "follow_up": c.follow_up,
                        "supplement_deadline": c.supplement_deadline,
                    }
                    for c in sorted(claims, key=lambda c: c.submitted_at)
                ],
                "generated_at": self._now_iso(),
            }

    # ---------- 补件跟进 ----------

    def _sweep_locked(self, now: datetime) -> list[str]:
        """补件期限到达时自动标记需要跟进的案件。"""
        flagged = []
        for claim in self.claims.values():
            if (
                claim.status == "needs_documents"
                and claim.supplement_deadline
                and not claim.follow_up
                and to_dt(claim.supplement_deadline) <= now
            ):
                claim.follow_up = True
                claim.follow_up_at = to_iso(now)
                claim.history.append(
                    {"at": to_iso(now), "event": "follow_up_flagged", "by": "system"}
                )
                self._emit(
                    EVT_FOLLOW_UP,
                    "claim",
                    claim.claim_id,
                    {
                        "receipt_id": claim.receipt_id,
                        "supplement_deadline": claim.supplement_deadline,
                    },
                    actor="system",
                    at=to_iso(now),
                )
                flagged.append(claim.claim_id)
        if flagged:
            self._save()
        return flagged

    def sweep(self, *, as_of=None) -> list[str]:
        with self._lock:
            now = to_dt(parse_datetime(as_of, "as_of")) if as_of else self._now()
            return self._sweep_locked(now)

    def list_follow_ups(self) -> list[dict]:
        with self._lock:
            self._sweep_locked(self._now())
            result = []
            for claim in self.claims.values():
                if not claim.follow_up:
                    continue
                missing = sorted(
                    {
                        doc
                        for p in claim.portions
                        if p.category == CATEGORY_PENDING
                        for doc in p.basis.get("missing_documents", [])
                    }
                )
                receipt = self.receipts.get(claim.receipt_id)
                result.append(
                    {
                        "claim_id": claim.claim_id,
                        "player_id": claim.player_id,
                        "course_id": claim.course_id,
                        "receipt_id": claim.receipt_id,
                        "receipt_no": receipt.receipt_no if receipt else None,
                        "status": claim.status,
                        "pending_amount": claim.totals[CATEGORY_PENDING],
                        "missing_documents": missing,
                        "supplement_deadline": claim.supplement_deadline,
                        "follow_up_at": claim.follow_up_at,
                    }
                )
            result.sort(key=lambda r: r["supplement_deadline"] or "")
            return result
