"""理赔账本引擎。

核心规则：
- 报销顺序由系统强制执行：商业保险 -> 俱乐部福利 -> 个人账户，前台无法填错；
- 一笔费用拆分为 可报(reimbursable) / 待补件(pending_documents) /
  个人承担(personal) / 已拒赔(denied) 四类去向(allocation)；
- 预授权过期、治疗方案变更、跨年度保单切换都会写入带决定时点的事件；
- 收据版本只增不改，供应商补传不会覆盖原始影像；
- 同一张收据或同一外部单号重复索赔时，返回原申请及其当前状态；
- 免赔额与额度消耗以追加台账(consumptions)记录，决定不可篡改，只能补充。
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP

from domain import (
    UNIT,
    ConflictError,
    NotFoundError,
    ValidationError,
    optional_str,
    parse_date,
    parse_quantity,
    parse_ratio,
    require_list,
    require_str,
    str_list,
    to_cents,
    to_iso,
    utcnow,
)
from store import JsonStore

PAYER_TYPES = ("insurance", "club")
PAYER_ORDER = {"insurance": 0, "club": 1}
DEFAULT_PRIORITY = {"insurance": 10, "club": 20}
DEFAULT_DOCUMENT_DEADLINE_DAYS = 30

BUCKET_REIMBURSABLE = "reimbursable"
BUCKET_PENDING = "pending_documents"
BUCKET_PERSONAL = "personal"
BUCKET_DENIED = "denied"
BUCKETS = (BUCKET_REIMBURSABLE, BUCKET_PENDING, BUCKET_PERSONAL, BUCKET_DENIED)

ID_PREFIXES = {
    "player": "plr",
    "course": "crs",
    "policy": "pol",
    "prescription": "prx",
    "pre_authorization": "pau",
    "receipt": "rcp",
    "claim": "clm",
    "payment_batch": "bat",
    "event": "evt",
    "consumption": "con",
    "allocation": "alc",
}


class LedgerEngine:
    def __init__(self, store: JsonStore, now_fn=None) -> None:
        self.store = store
        self._now_fn = now_fn or utcnow
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # 基础设施
    # ------------------------------------------------------------------
    def _now(self) -> datetime:
        return self._now_fn()

    def _now_iso(self) -> str:
        return to_iso(self._now())

    def _next_id(self, kind: str) -> str:
        counters = self.store.data["counters"]
        counters[kind] = counters.get(kind, 0) + 1
        return f"{ID_PREFIXES[kind]}_{counters[kind]:06d}"

    def _emit(self, event_type: str, entity_type: str, entity_id: str,
              detail: dict | None = None, actor: str | None = None) -> dict:
        event = {
            "id": self._next_id("event"),
            "type": event_type,
            "at": self._now_iso(),
            "entity_type": entity_type,
            "entity_id": entity_id,
            "actor": actor,
            "detail": detail or {},
        }
        self.store.data["events"].append(event)
        return event

    def _get(self, collection: str, entity_id: str, label: str) -> dict:
        entity = self.store.data[collection].get(entity_id)
        if entity is None:
            raise NotFoundError(f"{label}不存在：{entity_id}")
        return entity

    def list_events(self, entity_type: str | None = None,
                    entity_id: str | None = None) -> list[dict]:
        events = self.store.data["events"]
        if entity_type is not None:
            events = [e for e in events if e["entity_type"] == entity_type]
        if entity_id is not None:
            events = [e for e in events if e["entity_id"] == entity_id]
        return list(events)

    # ------------------------------------------------------------------
    # 基础档案：球员 / 疗程 / 保单
    # ------------------------------------------------------------------
    def create_player(self, payload: dict) -> dict:
        with self._lock:
            player = {
                "id": self._next_id("player"),
                "name": require_str(payload, "name"),
                "team": optional_str(payload, "team"),
                "created_at": self._now_iso(),
            }
            self.store.data["players"][player["id"]] = player
            self.store.save()
            return player

    def get_player(self, player_id: str) -> dict:
        return self._get("players", player_id, "球员")

    def create_course(self, payload: dict) -> dict:
        with self._lock:
            player = self._get("players", require_str(payload, "player_id"), "球员")
            course = {
                "id": self._next_id("course"),
                "player_id": player["id"],
                "diagnosis": require_str(payload, "diagnosis"),
                "status": "open",
                "opened_at": self._now_iso(),
                "last_insurance_policy_id": None,
            }
            self.store.data["courses"][course["id"]] = course
            self._emit("course_opened", "course", course["id"],
                       {"player_id": player["id"], "diagnosis": course["diagnosis"]})
            self.store.save()
            return course

    def create_policy(self, payload: dict) -> dict:
        with self._lock:
            player = self._get("players", require_str(payload, "player_id"), "球员")
            payer_type = require_str(payload, "payer_type")
            if payer_type not in PAYER_TYPES:
                raise ValidationError(
                    f"payer_type 必须是 {list(PAYER_TYPES)} 之一", "payer_type")
            effective_from = parse_date(require_str(payload, "effective_from"), "effective_from")
            effective_to = parse_date(require_str(payload, "effective_to"), "effective_to")
            if effective_from > effective_to:
                raise ValidationError("保单生效日不能晚于截止日", "effective_to")
            annual_limit = payload.get("annual_limit")
            deadline_days = payload.get("document_deadline_days", DEFAULT_DOCUMENT_DEADLINE_DAYS)
            if not isinstance(deadline_days, int) or deadline_days <= 0:
                raise ValidationError("document_deadline_days 必须是正整数",
                                      "document_deadline_days")
            policy = {
                "id": self._next_id("policy"),
                "player_id": player["id"],
                "payer_type": payer_type,
                "name": require_str(payload, "name"),
                "priority": int(payload.get("priority", DEFAULT_PRIORITY[payer_type])),
                "effective_from": effective_from.isoformat(),
                "effective_to": effective_to.isoformat(),
                "deductible_cents": to_cents(payload.get("deductible", 0), "deductible"),
                "annual_limit_cents": (
                    None if annual_limit is None else to_cents(annual_limit, "annual_limit")
                ),
                "coverage_ratio": str(parse_ratio(payload.get("coverage_ratio", 1))),
                "covered_categories": str_list(payload, "covered_categories"),
                "excluded_categories": str_list(payload, "excluded_categories"),
                "preauth_required_categories": str_list(payload, "preauth_required_categories"),
                "document_deadline_days": deadline_days,
                "created_at": self._now_iso(),
            }
            self.store.data["policies"][policy["id"]] = policy
            self.store.save()
            return policy

    # ------------------------------------------------------------------
    # 处方（治疗方案）：版本化，变更留下决定时点
    # ------------------------------------------------------------------
    def _active_prescription(self, course_id: str) -> dict | None:
        for prescription in self.store.data["prescriptions"].values():
            if prescription["course_id"] == course_id and prescription["status"] == "active":
                return prescription
        return None

    def _create_prescription_version(self, course: dict, items: list,
                                     reason: str, actor: str | None) -> dict:
        version = 1
        for prescription in self.store.data["prescriptions"].values():
            if prescription["course_id"] == course["id"]:
                version = max(version, prescription["version"] + 1)
                prescription["status"] = "superseded"
        normalized = []
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                raise ValidationError(f"处方第 {index + 1} 项格式不正确", "items")
            normalized.append({
                "service_code": require_str(item, "service_code"),
                "category": require_str(item, "category"),
                "quantity": str(parse_quantity(item.get("quantity", 1))),
                "unit_price_cents": to_cents(item.get("unit_price", 0), "unit_price"),
            })
        prescription = {
            "id": self._next_id("prescription"),
            "course_id": course["id"],
            "version": version,
            "items": normalized,
            "status": "active",
            "reason": reason,
            "created_at": self._now_iso(),
        }
        self.store.data["prescriptions"][prescription["id"]] = prescription
        self._emit("prescription", "prescription", prescription["id"],
                   {"course_id": course["id"], "version": version, "reason": reason},
                   actor=actor)
        return prescription

    def create_prescription(self, payload: dict) -> dict:
        with self._lock:
            course = self._get("courses", require_str(payload, "course_id"), "疗程")
            if self._active_prescription(course["id"]) is not None:
                raise ValidationError("该疗程已有生效处方，请使用修订接口变更治疗方案",
                                      "course_id")
            prescription = self._create_prescription_version(
                course, require_list(payload, "items"),
                optional_str(payload, "reason") or "初始治疗方案",
                optional_str(payload, "actor"))
            self.store.save()
            return prescription

    def revise_prescription(self, prescription_id: str, payload: dict) -> dict:
        """治疗方案变更：作废旧版本，新版本留下决定时点。"""
        with self._lock:
            previous = self._get("prescriptions", prescription_id, "处方")
            course = self._get("courses", previous["course_id"], "疗程")
            prescription = self._create_prescription_version(
                course, require_list(payload, "items"),
                require_str(payload, "reason"), optional_str(payload, "actor"))
            self._emit("plan_changed", "course", course["id"], {
                "from_prescription_id": previous["id"],
                "to_prescription_id": prescription["id"],
                "decided_at": self._now_iso(),
                "reason": prescription["reason"],
            }, actor=optional_str(payload, "actor"))
            self.store.save()
            return prescription

    # ------------------------------------------------------------------
    # 预授权
    # ------------------------------------------------------------------
    def create_pre_authorization(self, payload: dict) -> dict:
        with self._lock:
            preauth = self._create_pre_authorization(payload)
            self.store.save()
            return preauth

    def _create_pre_authorization(self, payload: dict) -> dict:
        course = self._get("courses", require_str(payload, "course_id"), "疗程")
        policy = self._get("policies", require_str(payload, "policy_id"), "保单")
        valid_from = parse_date(require_str(payload, "valid_from"), "valid_from")
        valid_to = parse_date(require_str(payload, "valid_to"), "valid_to")
        if valid_from > valid_to:
            raise ValidationError("预授权生效日不能晚于截止日", "valid_to")
        preauth = {
            "id": self._next_id("pre_authorization"),
            "course_id": course["id"],
            "policy_id": policy["id"],
            "categories": str_list(payload, "categories"),
            "approved_amount_cents": to_cents(
                require_str(payload, "approved_amount"), "approved_amount"),
            "used_cents": 0,
            "valid_from": valid_from.isoformat(),
            "valid_to": valid_to.isoformat(),
            "reference": optional_str(payload, "reference"),
            "created_at": self._now_iso(),
        }
        self.store.data["pre_authorizations"][preauth["id"]] = preauth
        self._emit("pre_authorization", "pre_authorization", preauth["id"],
                   {"course_id": course["id"], "policy_id": policy["id"],
                    "valid_to": preauth["valid_to"]})
        return preauth

    # ------------------------------------------------------------------
    # 收据：版本只增不改，补传不覆盖原始影像
    # ------------------------------------------------------------------
    def register_receipt(self, payload: dict) -> dict:
        with self._lock:
            player = self._get("players", require_str(payload, "player_id"), "球员")
            receipt_no = require_str(payload, "receipt_no")
            provider = require_str(payload, "provider")
            for existing in self.store.data["receipts"].values():
                if existing["receipt_no"] == receipt_no and existing["provider"] == provider:
                    raise ConflictError("同一供应商的该票号已登记", {
                        "error": "duplicate_receipt",
                        "original_receipt_id": existing["id"],
                        "original_receipt": existing,
                    })
            receipt = {
                "id": self._next_id("receipt"),
                "player_id": player["id"],
                "receipt_no": receipt_no,
                "provider": provider,
                "versions": [{
                    "version": 1,
                    "kind": "original",
                    "image_uri": require_str(payload, "image_uri"),
                    "uploaded_by": optional_str(payload, "uploaded_by") or "clinic",
                    "uploaded_at": self._now_iso(),
                    "note": optional_str(payload, "note"),
                }],
                "created_at": self._now_iso(),
            }
            self.store.data["receipts"][receipt["id"]] = receipt
            self._emit("receipt", "receipt", receipt["id"],
                       {"receipt_no": receipt_no, "provider": provider})
            self.store.save()
            return receipt

    def add_receipt_version(self, receipt_id: str, payload: dict) -> dict:
        """供应商补传：追加新版本，原始影像永远保留在 versions[0]。"""
        with self._lock:
            receipt = self._get("receipts", receipt_id, "收据")
            version = len(receipt["versions"]) + 1
            receipt["versions"].append({
                "version": version,
                "kind": "supplement",
                "image_uri": require_str(payload, "image_uri"),
                "uploaded_by": optional_str(payload, "uploaded_by") or "supplier",
                "uploaded_at": self._now_iso(),
                "note": optional_str(payload, "note"),
            })
            self._emit("supplement", "receipt", receipt["id"],
                       {"version": version,
                        "uploaded_by": receipt["versions"][-1]["uploaded_by"]})
            self.store.save()
            return receipt

    def get_receipt(self, receipt_id: str) -> dict:
        return self._get("receipts", receipt_id, "收据")

    # ------------------------------------------------------------------
    # 理赔提交与费用拆分
    # ------------------------------------------------------------------
    def submit_claim(self, payload: dict) -> dict:
        with self._lock:
            course = self._get("courses", require_str(payload, "course_id"), "疗程")
            player_id = course["player_id"]
            external_ref = optional_str(payload, "external_ref")
            if external_ref is not None:
                for existing in self.store.data["claims"].values():
                    if existing.get("external_ref") == external_ref:
                        raise self._duplicate_claim(existing, "external_ref")

            prescription_id = optional_str(payload, "prescription_id")
            if prescription_id is not None:
                prescription = self._get("prescriptions", prescription_id, "处方")
                if prescription["course_id"] != course["id"]:
                    raise ValidationError("处方不属于该疗程", "prescription_id")
            else:
                prescription = self._active_prescription(course["id"])
                if prescription is None:
                    raise ValidationError("该疗程还没有生效处方", "course_id")
            prescription_items = {item["service_code"]: item
                                  for item in prescription["items"]}

            raw_lines = require_list(payload, "lines")
            lines = []
            for index, raw in enumerate(raw_lines):
                if not isinstance(raw, dict):
                    raise ValidationError(f"第 {index + 1} 行费用格式不正确", "lines")
                quantity = parse_quantity(raw.get("quantity", 1))
                unit_price_cents = to_cents(
                    require_str(raw, "unit_price"), "unit_price")
                amount_cents = int(
                    (Decimal(unit_price_cents) * quantity).quantize(
                        UNIT, rounding=ROUND_HALF_UP))
                receipt_id = optional_str(raw, "receipt_id")
                if receipt_id is not None:
                    self._get("receipts", receipt_id, "收据")
                lines.append({
                    "line_id": f"ln_{index + 1}",
                    "service_code": require_str(raw, "service_code"),
                    "quantity": str(quantity),
                    "unit_price_cents": unit_price_cents,
                    "amount_cents": amount_cents,
                    "service_date": parse_date(
                        require_str(raw, "service_date"), "service_date").isoformat(),
                    "receipt_id": receipt_id,
                    "category": None,
                    "allocations": [],
                })

            # 同一张票据不能被重复使用：既不能在别的申请里，也不能在本申请内重复
            seen_receipts = set()
            for line in lines:
                if line["receipt_id"] is None:
                    continue
                if line["receipt_id"] in seen_receipts:
                    raise ValidationError(
                        f"收据 {line['receipt_id']} 在本次申请中被重复引用", "lines")
                seen_receipts.add(line["receipt_id"])
                for existing in self.store.data["claims"].values():
                    for used in existing["lines"]:
                        if used.get("receipt_id") == line["receipt_id"]:
                            raise self._duplicate_claim(
                                existing, "receipt", receipt_id=line["receipt_id"])

            claim = {
                "id": self._next_id("claim"),
                "external_ref": external_ref,
                "course_id": course["id"],
                "player_id": player_id,
                "prescription_id": prescription["id"],
                "prescription_version": prescription["version"],
                "status": "draft",
                "lines": lines,
                "supplement_deadline": None,
                "follow_up_required": False,
                "follow_up_flagged_at": None,
                "submitted_by": optional_str(payload, "submitted_by"),
                "created_at": self._now_iso(),
                "updated_at": self._now_iso(),
            }
            now_iso = self._now_iso()
            pending_deadline_days = []
            for line in claim["lines"]:
                line["allocations"] = self._split_line(
                    claim, course, line, prescription_items, now_iso)
                for allocation in line["allocations"]:
                    if allocation["bucket"] == BUCKET_PENDING:
                        policy = self.store.data["policies"].get(
                            allocation.get("policy_id") or "")
                        days = (policy or {}).get(
                            "document_deadline_days", DEFAULT_DOCUMENT_DEADLINE_DAYS)
                        pending_deadline_days.append(days)

            if pending_deadline_days:
                deadline = self._now() + timedelta(days=max(pending_deadline_days))
                claim["supplement_deadline"] = to_iso(deadline)

            claim["status"] = self._claim_status(claim)
            self.store.data["claims"][claim["id"]] = claim
            self._record_policy_switch(course, claim)
            self._emit("claim_submitted", "claim", claim["id"], {
                "course_id": course["id"],
                "status": claim["status"],
                "summary": self._claim_summary(claim)["totals"],
            }, actor=claim["submitted_by"])
            self._emit_decision_event(claim)
            self.store.save()
            return self.get_claim(claim["id"])

    def _duplicate_claim(self, existing: dict, matched_on: str,
                         receipt_id: str | None = None) -> ConflictError:
        payload = {
            "error": "duplicate_claim",
            "matched_on": matched_on,
            "original_claim_id": existing["id"],
            "status": existing["status"],
            "original_claim": existing,
        }
        if receipt_id is not None:
            payload["receipt_id"] = receipt_id
        return ConflictError("重复索赔：已存在相同申请", payload)

    def _applicable_policies(self, player_id: str, service_date: str) -> list[dict]:
        day = parse_date(service_date, "service_date")
        policies = []
        for policy in self.store.data["policies"].values():
            if policy["player_id"] != player_id:
                continue
            if parse_date(policy["effective_from"], "effective_from") <= day <= \
                    parse_date(policy["effective_to"], "effective_to"):
                policies.append(policy)
        policies.sort(key=lambda p: (PAYER_ORDER[p["payer_type"]], p["priority"], p["created_at"]))
        return policies

    def _policy_usage(self, policy_id: str, course_id: str | None = None) -> tuple[int, int]:
        deductible = 0
        limit = 0
        for consumption in self.store.data["consumptions"]:
            if consumption["policy_id"] != policy_id:
                continue
            if course_id is not None and consumption["course_id"] != course_id:
                continue
            deductible += consumption["deductible_cents"]
            limit += consumption["limit_cents"]
        return deductible, limit

    def _record_consumption(self, claim: dict, line: dict, policy: dict,
                            deductible_cents: int, limit_cents: int) -> None:
        if deductible_cents == 0 and limit_cents == 0:
            return
        self.store.data["consumptions"].append({
            "id": self._next_id("consumption"),
            "policy_id": policy["id"],
            "player_id": claim["player_id"],
            "course_id": claim["course_id"],
            "claim_id": claim["id"],
            "line_id": line["line_id"],
            "deductible_cents": deductible_cents,
            "limit_cents": limit_cents,
            "decided_at": self._now_iso(),
        })

    def _find_preauth(self, course_id: str, policy: dict,
                      category: str, service_date: str) -> tuple[dict | None, str]:
        day = parse_date(service_date, "service_date")
        candidates = []
        for preauth in self.store.data["pre_authorizations"].values():
            if preauth["course_id"] != course_id or preauth["policy_id"] != policy["id"]:
                continue
            if preauth["categories"] and category not in preauth["categories"]:
                continue
            candidates.append(preauth)
        if not candidates:
            return None, "missing"
        for preauth in candidates:
            if parse_date(preauth["valid_from"], "valid_from") <= day <= \
                    parse_date(preauth["valid_to"], "valid_to"):
                return preauth, "valid"
        return None, "expired"

    def _split_line(self, claim: dict, course: dict, line: dict,
                    prescription_items: dict, now_iso: str) -> list[dict]:
        """把一行费用拆成去向。只追加新决定，不改写历史。"""
        allocations = []

        def add(bucket, amount_cents, reason, policy=None, payer_type=None,
                basis=None):
            allocations.append({
                "id": self._next_id("allocation"),
                "bucket": bucket,
                "payer_type": payer_type,
                "policy_id": policy["id"] if policy else None,
                "amount_cents": amount_cents,
                "reason": reason,
                "basis": basis,
                "decided_at": now_iso,
                "resolved": False,
                "paid": False,
                "paid_batch_id": None,
            })

        amount = line["amount_cents"]
        item = prescription_items.get(line["service_code"])
        if item is None:
            add(BUCKET_DENIED, amount, "not_in_prescription")
            return allocations
        line["category"] = item["category"]
        category = item["category"]

        receipt = None
        if line.get("receipt_id"):
            receipt = self.store.data["receipts"].get(line["receipt_id"])
        if receipt is None:
            add(BUCKET_PENDING, amount, "receipt_missing")
            return allocations
        receipt_version = receipt["versions"][-1]["version"]

        policies = self._applicable_policies(claim["player_id"], line["service_date"])
        if not policies:
            add(BUCKET_PERSONAL, amount, "no_active_policy")
            return allocations

        # 主商业保险的预授权检查：缺失或过期先挂起整行，等待补件
        primary_insurance = next(
            (p for p in policies if p["payer_type"] == "insurance"), None)
        preauth = None
        if primary_insurance is not None and \
                category in primary_insurance["preauth_required_categories"]:
            preauth, state = self._find_preauth(
                course["id"], primary_insurance, category, line["service_date"])
            if state == "missing":
                add(BUCKET_PENDING, amount, "pre_authorization_missing",
                    policy=primary_insurance)
                return allocations
            if state == "expired":
                self._emit("pre_authorization_expired", "claim", claim["id"], {
                    "line_id": line["line_id"],
                    "policy_id": primary_insurance["id"],
                    "category": category,
                    "service_date": line["service_date"],
                    "decided_at": now_iso,
                })
                add(BUCKET_PENDING, amount, "pre_authorization_expired",
                    policy=primary_insurance)
                return allocations

        pool = amount
        excluded_by = []
        for policy in policies:
            if pool <= 0:
                break
            if category in policy["excluded_categories"]:
                excluded_by.append(policy["id"])
                continue
            covered = policy["covered_categories"]
            if covered and category not in covered:
                continue

            deductible_used, limit_used = self._policy_usage(policy["id"])
            deductible_remaining = max(0, policy["deductible_cents"] - deductible_used)
            deductible_applied = min(pool, deductible_remaining)
            after_deductible = pool - deductible_applied
            ratio = Decimal(policy["coverage_ratio"])
            cover = int((Decimal(after_deductible) * ratio).quantize(
                UNIT, rounding=ROUND_HALF_UP))
            if policy["annual_limit_cents"] is None:
                pay = cover
            else:
                limit_remaining = max(0, policy["annual_limit_cents"] - limit_used)
                pay = min(cover, limit_remaining)
            if preauth is not None and policy["id"] == primary_insurance["id"]:
                preauth_remaining = max(
                    0, preauth["approved_amount_cents"] - preauth["used_cents"])
                pay = min(pay, preauth_remaining)

            self._record_consumption(claim, line, policy, deductible_applied, pay)
            if preauth is not None and policy["id"] == primary_insurance["id"]:
                preauth["used_cents"] += pay
            if pay > 0:
                add(BUCKET_REIMBURSABLE, pay, "covered", policy=policy,
                    payer_type=policy["payer_type"],
                    basis={
                        "policy_id": policy["id"],
                        "policy_name": policy["name"],
                        "payer_type": policy["payer_type"],
                        "coverage_ratio": policy["coverage_ratio"],
                        "deductible_applied_cents": deductible_applied,
                        "preauth_id": preauth["id"] if preauth and
                            policy["id"] == primary_insurance["id"] else None,
                        "receipt_id": receipt["id"],
                        "receipt_version": receipt_version,
                    })
                pool -= pay
            # 免赔额部分不由该保单支付，继续流向下一支付方

        if pool > 0:
            if pool == amount and excluded_by:
                add(BUCKET_DENIED, pool, "service_excluded",
                    basis={"excluded_by_policy_ids": excluded_by})
            else:
                reason = "patient_share" if pool < amount else "not_covered"
                add(BUCKET_PERSONAL, pool, reason)
        return allocations

    def _claim_status(self, claim: dict) -> str:
        has_pending = False
        has_reimbursable = False
        all_paid = True
        for line in claim["lines"]:
            for allocation in line["allocations"]:
                if allocation.get("resolved"):
                    continue
                if allocation["bucket"] == BUCKET_PENDING:
                    has_pending = True
                if allocation["bucket"] == BUCKET_REIMBURSABLE:
                    has_reimbursable = True
                    if not allocation["paid"]:
                        all_paid = False
        if has_pending:
            return "needs_documents"
        if has_reimbursable:
            return "paid" if all_paid else "approved"
        return "denied"

    def _record_policy_switch(self, course: dict, claim: dict) -> None:
        insurance_policy_ids = []
        for line in claim["lines"]:
            for allocation in line["allocations"]:
                if allocation.get("resolved"):
                    continue
                if allocation["payer_type"] == "insurance" and allocation["policy_id"]:
                    if allocation["policy_id"] not in insurance_policy_ids:
                        insurance_policy_ids.append(allocation["policy_id"])
        if not insurance_policy_ids:
            return
        current = insurance_policy_ids[0]
        previous = course.get("last_insurance_policy_id")
        if previous and previous != current:
            self._emit("policy_year_switched", "course", course["id"], {
                "claim_id": claim["id"],
                "from_policy_id": previous,
                "to_policy_id": current,
                "decided_at": self._now_iso(),
            })
        course["last_insurance_policy_id"] = current

    def _emit_decision_event(self, claim: dict) -> None:
        if claim["status"] in ("approved", "paid"):
            self._emit("approval", "claim", claim["id"],
                       {"summary": self._claim_summary(claim)["totals"]})
        elif claim["status"] == "denied":
            self._emit("denial", "claim", claim["id"],
                       {"summary": self._claim_summary(claim)["totals"]})

    # ------------------------------------------------------------------
    # 补件与重审
    # ------------------------------------------------------------------
    def supplement_claim(self, claim_id: str, payload: dict) -> dict:
        """供应商/队医补件：登记补充材料并重审待补件部分。"""
        with self._lock:
            claim = self._get("claims", claim_id, "理赔申请")
            course = self._get("courses", claim["course_id"], "疗程")
            pending_lines = [
                line for line in claim["lines"]
                if any(a["bucket"] == BUCKET_PENDING and not a.get("resolved")
                       for a in line["allocations"])
            ]
            if not pending_lines:
                raise ValidationError("该申请没有待补件部分", "claim_id")

            preauth_payload = payload.get("pre_authorization")
            if preauth_payload is not None:
                preauth_payload = dict(preauth_payload)
                preauth_payload.setdefault("course_id", claim["course_id"])
                self._create_pre_authorization(preauth_payload)

            for link in payload.get("receipt_links", []):
                line_id = require_str(link, "line_id")
                receipt_id = require_str(link, "receipt_id")
                self._get("receipts", receipt_id, "收据")
                line = next((l for l in claim["lines"] if l["line_id"] == line_id), None)
                if line is None:
                    raise ValidationError(f"申请中不存在费用行：{line_id}", "line_id")
                if line.get("receipt_id") and line["receipt_id"] != receipt_id:
                    raise ValidationError(
                        f"费用行 {line_id} 已关联收据，不能替换原始票据", "line_id")
                line["receipt_id"] = receipt_id

            prescription = self._get("prescriptions", claim["prescription_id"], "处方")
            prescription_items = {item["service_code"]: item
                                  for item in prescription["items"]}
            now_iso = self._now_iso()
            for line in pending_lines:
                for allocation in line["allocations"]:
                    if allocation["bucket"] == BUCKET_PENDING and not allocation.get("resolved"):
                        allocation["resolved"] = True
                        allocation["resolved_at"] = now_iso
                line["allocations"].extend(self._split_line(
                    claim, course, line, prescription_items, now_iso))

            claim["status"] = self._claim_status(claim)
            if claim["status"] != "needs_documents":
                claim["follow_up_required"] = False
                claim["follow_up_flagged_at"] = None
            claim["updated_at"] = now_iso
            self._emit("supplement", "claim", claim["id"], {
                "note": optional_str(payload, "note"),
                "status": claim["status"],
            }, actor=optional_str(payload, "uploaded_by"))
            self._emit_decision_event(claim)
            self.store.save()
            return self.get_claim(claim["id"])

    # ------------------------------------------------------------------
    # 查询：申请详情 / 免赔额与额度
    # ------------------------------------------------------------------
    def _claim_summary(self, claim: dict) -> dict:
        totals = {bucket: 0 for bucket in BUCKETS}
        paid_cents = 0
        for line in claim["lines"]:
            for allocation in line["allocations"]:
                if allocation.get("resolved"):
                    continue
                totals[allocation["bucket"]] += allocation["amount_cents"]
                if allocation["bucket"] == BUCKET_REIMBURSABLE and allocation["paid"]:
                    paid_cents += allocation["amount_cents"]
        return {"totals": totals, "paid_cents": paid_cents}

    def get_claim(self, claim_id: str) -> dict:
        claim = self._get("claims", claim_id, "理赔申请")
        result = dict(claim)
        result["summary"] = self._claim_summary(claim)
        return result

    def coverage_view(self, player_id: str, course_id: str) -> dict:
        """队医视角：按球员 + 疗程查看累计免赔额与剩余额度。"""
        player = self._get("players", player_id, "球员")
        course = self._get("courses", course_id, "疗程")
        if course["player_id"] != player["id"]:
            raise ValidationError("疗程不属于该球员", "course_id")

        policies = []
        for policy in self.store.data["policies"].values():
            if policy["player_id"] != player_id:
                continue
            ded_total, lim_total = self._policy_usage(policy["id"])
            ded_course, lim_course = self._policy_usage(policy["id"], course_id)
            limit = policy["annual_limit_cents"]
            policies.append({
                "policy_id": policy["id"],
                "name": policy["name"],
                "payer_type": policy["payer_type"],
                "effective_from": policy["effective_from"],
                "effective_to": policy["effective_to"],
                "deductible_cents": policy["deductible_cents"],
                "deductible_used_cents": ded_total,
                "deductible_used_in_course_cents": ded_course,
                "deductible_remaining_cents": max(0, policy["deductible_cents"] - ded_total),
                "annual_limit_cents": limit,
                "limit_used_cents": lim_total,
                "limit_used_in_course_cents": lim_course,
                "limit_remaining_cents": None if limit is None else max(0, limit - lim_total),
            })

        totals = {bucket: 0 for bucket in BUCKETS}
        claims = []
        for claim in self.store.data["claims"].values():
            if claim["course_id"] != course_id:
                continue
            summary = self._claim_summary(claim)
            for bucket, cents in summary["totals"].items():
                totals[bucket] += cents
            claims.append({"claim_id": claim["id"], "status": claim["status"],
                           "totals": summary["totals"]})
        return {
            "player_id": player_id,
            "course_id": course_id,
            "diagnosis": course["diagnosis"],
            "policies": policies,
            "totals": totals,
            "claims": claims,
        }

    # ------------------------------------------------------------------
    # 付款批次与责任反查
    # ------------------------------------------------------------------
    def create_payment_batch(self, payload: dict) -> dict:
        with self._lock:
            policy_id = optional_str(payload, "policy_id")
            payer_type = optional_str(payload, "payer_type")
            if policy_id is None and payer_type is None:
                raise ValidationError("必须指定 policy_id 或 payer_type", "policy_id")
            if policy_id is not None:
                policy = self._get("policies", policy_id, "保单")
                payer_type = policy["payer_type"]
            if payer_type not in PAYER_TYPES:
                raise ValidationError(
                    f"payer_type 必须是 {list(PAYER_TYPES)} 之一", "payer_type")
            claim_filter = set(payload.get("claim_ids") or [])

            selected = []
            for claim in self.store.data["claims"].values():
                if claim_filter and claim["id"] not in claim_filter:
                    continue
                for line in claim["lines"]:
                    for allocation in line["allocations"]:
                        if allocation.get("resolved") or allocation["paid"]:
                            continue
                        if allocation["bucket"] != BUCKET_REIMBURSABLE:
                            continue
                        if policy_id is not None and allocation["policy_id"] != policy_id:
                            continue
                        if policy_id is None and allocation["payer_type"] != payer_type:
                            continue
                        selected.append((claim, line, allocation))
            if not selected:
                raise ValidationError("没有可付款的已批费用", "claim_ids")

            batch = {
                "id": self._next_id("payment_batch"),
                "payer_type": payer_type,
                "policy_id": policy_id,
                "reference": optional_str(payload, "reference"),
                "lines": [],
                "total_cents": 0,
                "created_at": self._now_iso(),
            }
            touched_claims = set()
            for claim, line, allocation in selected:
                basis = dict(allocation.get("basis") or {})
                batch["lines"].append({
                    "claim_id": claim["id"],
                    "line_id": line["line_id"],
                    "allocation_id": allocation["id"],
                    "service_code": line["service_code"],
                    "service_date": line["service_date"],
                    "receipt_id": line.get("receipt_id"),
                    "amount_cents": allocation["amount_cents"],
                    "basis": basis,
                    "decided_at": allocation["decided_at"],
                })
                batch["total_cents"] += allocation["amount_cents"]
                allocation["paid"] = True
                allocation["paid_batch_id"] = batch["id"]
                touched_claims.add(claim["id"])

            for claim_id in sorted(touched_claims):
                claim = self.store.data["claims"][claim_id]
                new_status = self._claim_status(claim)
                if new_status != claim["status"]:
                    claim["status"] = new_status
                    claim["updated_at"] = self._now_iso()

            self.store.data["payment_batches"][batch["id"]] = batch
            self._emit("payout", "payment_batch", batch["id"], {
                "payer_type": payer_type,
                "policy_id": policy_id,
                "total_cents": batch["total_cents"],
                "claim_ids": sorted(touched_claims),
            })
            self.store.save()
            return batch

    def get_payment_batch(self, batch_id: str) -> dict:
        return self._get("payment_batches", batch_id, "付款批次")

    def trace_payment_batch(self, batch_id: str) -> dict:
        """会计视角：从付款批次反查每张收据的责任依据。"""
        batch = self.get_payment_batch(batch_id)
        receipts: dict[str, dict] = {}
        for line in batch["lines"]:
            receipt_id = line.get("receipt_id")
            key = receipt_id or "(无收据)"
            entry = receipts.setdefault(key, {
                "receipt_id": receipt_id,
                "receipt_no": None,
                "provider": None,
                "versions": [],
                "liabilities": [],
                "total_cents": 0,
            })
            if receipt_id is not None:
                receipt = self.store.data["receipts"].get(receipt_id)
                if receipt is not None:
                    entry["receipt_no"] = receipt["receipt_no"]
                    entry["provider"] = receipt["provider"]
                    entry["versions"] = receipt["versions"]
            entry["liabilities"].append({
                "claim_id": line["claim_id"],
                "line_id": line["line_id"],
                "service_code": line["service_code"],
                "service_date": line["service_date"],
                "amount_cents": line["amount_cents"],
                "policy_id": line["basis"].get("policy_id"),
                "policy_name": line["basis"].get("policy_name"),
                "payer_type": line["basis"].get("payer_type"),
                "coverage_ratio": line["basis"].get("coverage_ratio"),
                "deductible_applied_cents": line["basis"].get("deductible_applied_cents"),
                "preauth_id": line["basis"].get("preauth_id"),
                "receipt_version": line["basis"].get("receipt_version"),
                "decided_at": line["decided_at"],
            })
            entry["total_cents"] += line["amount_cents"]
        return {"batch": batch, "receipts": list(receipts.values())}

    # ------------------------------------------------------------------
    # 补件跟进
    # ------------------------------------------------------------------
    def scan_follow_ups(self) -> list[dict]:
        """补件期限到达的待补件案件自动标记为需要跟进。"""
        with self._lock:
            now_iso = self._now_iso()
            changed = False
            for claim in self.store.data["claims"].values():
                if claim["status"] != "needs_documents":
                    continue
                deadline = claim.get("supplement_deadline")
                if not deadline or deadline > now_iso:
                    continue
                if not claim["follow_up_required"]:
                    claim["follow_up_required"] = True
                    claim["follow_up_flagged_at"] = now_iso
                    self._emit("follow_up_flagged", "claim", claim["id"], {
                        "supplement_deadline": deadline,
                        "decided_at": now_iso,
                    })
                    changed = True
            if changed:
                self.store.save()
            return self._follow_up_list()

    def _follow_up_list(self) -> list[dict]:
        flagged = []
        for claim in self.store.data["claims"].values():
            if not claim["follow_up_required"]:
                continue
            pending = []
            for line in claim["lines"]:
                for allocation in line["allocations"]:
                    if allocation["bucket"] == BUCKET_PENDING and not allocation.get("resolved"):
                        pending.append({
                            "line_id": line["line_id"],
                            "reason": allocation["reason"],
                            "amount_cents": allocation["amount_cents"],
                        })
            flagged.append({
                "claim_id": claim["id"],
                "player_id": claim["player_id"],
                "course_id": claim["course_id"],
                "status": claim["status"],
                "supplement_deadline": claim["supplement_deadline"],
                "follow_up_flagged_at": claim["follow_up_flagged_at"],
                "pending": pending,
            })
        return flagged
