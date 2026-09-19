"""理赔账本领域模型。

约定：金额为两位小数字符串，数量为六位小数字符串，日期为 YYYY-MM-DD，
时间戳为 UTC ISO 8601。所有实体可往返序列化（to_dict / from_dict）。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass
class Player:
    player_id: str
    name: str
    team: str
    created_at: str

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Player":
        return cls(**d)


@dataclass
class TreatmentCourse:
    course_id: str
    player_id: str
    diagnosis: str
    started_at: str
    status: str  # active / closed
    created_at: str

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "TreatmentCourse":
        return cls(**d)


@dataclass
class PlanItem:
    service_code: str
    quantity: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class PlanRevision:
    """治疗方案的一个版本。decided_at 是方案变更的决定时点。"""

    revision: int
    items: list[PlanItem]
    reason: str
    effective_from: str
    decided_at: str

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "PlanRevision":
        return cls(
            revision=d["revision"],
            items=[PlanItem(**i) for i in d["items"]],
            reason=d["reason"],
            effective_from=d["effective_from"],
            decided_at=d["decided_at"],
        )


@dataclass
class Prescription:
    prescription_id: str
    course_id: str
    player_id: str
    revisions: list[PlanRevision]
    created_at: str

    def revision_at(self, service_date: str) -> PlanRevision | None:
        """返回服务日期当日生效的方案版本。"""
        eligible = [r for r in self.revisions if r.effective_from <= service_date]
        if not eligible:
            return None
        return max(eligible, key=lambda r: (r.effective_from, r.revision))

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Prescription":
        return cls(
            prescription_id=d["prescription_id"],
            course_id=d["course_id"],
            player_id=d["player_id"],
            revisions=[PlanRevision.from_dict(r) for r in d["revisions"]],
            created_at=d["created_at"],
        )


@dataclass
class CoverageRule:
    service_code: str  # 具体编码或 "*" 通配
    ratio: str
    requires_preauth: bool = False
    required_documents: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Policy:
    policy_id: str
    payer_type: str  # commercial_insurance / club_benefit / personal_account
    payer_name: str
    policy_year: str
    currency: str
    effective_from: str
    effective_to: str
    deductible: str
    annual_limit: str
    priority: int
    rules: list[CoverageRule]
    created_at: str

    def match(self, service_code: str) -> CoverageRule | None:
        for rule in self.rules:
            if rule.service_code == service_code:
                return rule
        for rule in self.rules:
            if rule.service_code == "*":
                return rule
        return None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Policy":
        return cls(
            policy_id=d["policy_id"],
            payer_type=d["payer_type"],
            payer_name=d["payer_name"],
            policy_year=d["policy_year"],
            currency=d["currency"],
            effective_from=d["effective_from"],
            effective_to=d["effective_to"],
            deductible=d["deductible"],
            annual_limit=d["annual_limit"],
            priority=d["priority"],
            rules=[CoverageRule(**r) for r in d["rules"]],
            created_at=d["created_at"],
        )


@dataclass
class PreAuthorization:
    preauth_id: str
    policy_id: str
    course_id: str
    player_id: str
    service_codes: list[str]  # 空列表表示不限项目
    max_amount: str
    valid_from: str
    valid_until: str
    status: str  # active / revoked
    created_at: str

    def expired_at(self, service_date: str) -> bool:
        return service_date > self.valid_until

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "PreAuthorization":
        return cls(**d)


@dataclass
class ReceiptLine:
    line_id: str
    service_code: str
    description: str
    quantity: str
    unit_price: str
    amount: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ReceiptImage:
    """收据影像版本。版本只增不改，原始影像（version 1）永不被覆盖。"""

    version: int
    kind: str  # original / supplement
    image_ref: str
    uploaded_by: str
    uploaded_at: str
    note: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Receipt:
    receipt_id: str
    receipt_key: str  # 幂等键：player_id::vendor::receipt_no
    player_id: str
    course_id: str
    vendor: str
    receipt_no: str
    currency: str
    service_date: str  # 服务时间
    received_at: str  # 接收时间
    lines: list[ReceiptLine]
    documents: list[str]
    images: list[ReceiptImage]
    created_at: str

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Receipt":
        return cls(
            receipt_id=d["receipt_id"],
            receipt_key=d["receipt_key"],
            player_id=d["player_id"],
            course_id=d["course_id"],
            vendor=d["vendor"],
            receipt_no=d["receipt_no"],
            currency=d["currency"],
            service_date=d["service_date"],
            received_at=d["received_at"],
            lines=[ReceiptLine(**line) for line in d["lines"]],
            documents=list(d["documents"]),
            images=[ReceiptImage(**img) for img in d["images"]],
            created_at=d["created_at"],
        )


# 费用归属类别
CATEGORY_REIMBURSABLE = "reimbursable"  # 可报
CATEGORY_PENDING = "pending_documents"  # 待补件
CATEGORY_PERSONAL = "personal"  # 个人承担
CATEGORY_DENIED = "denied"  # 已拒赔
CATEGORIES = (CATEGORY_REIMBURSABLE, CATEGORY_PENDING, CATEGORY_PERSONAL, CATEGORY_DENIED)


@dataclass
class Portion:
    """一条费用在某个支付方下的归属片段。basis 保存责任依据，供会计反查。"""

    portion_id: str
    line_id: str
    service_code: str
    payer_type: str | None  # None 表示计划层面拒赔
    policy_id: str | None
    category: str
    amount: str
    reason: str
    status: str  # approved / paid / pending / denied / personal
    basis: dict
    paid_batch_id: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Portion":
        return cls(**d)


@dataclass
class ClaimEffect:
    """一次理赔对累计器（免赔额 / 年度额度）的影响，重算时整体回滚。"""

    policy_id: str
    policy_year: str
    deductible_applied: str
    limit_reserved: str

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ClaimEffect":
        return cls(**d)


@dataclass
class Claim:
    claim_id: str
    receipt_id: str
    player_id: str
    course_id: str
    status: str  # submitted / needs_documents / approved / denied / paid
    portions: list[Portion]
    effects: list[ClaimEffect]
    totals: dict
    supplement_deadline: str | None
    follow_up: bool
    follow_up_at: str | None
    submitted_by: str
    submitted_at: str
    decided_at: str
    paid_at: str | None
    history: list[dict]

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Claim":
        return cls(
            claim_id=d["claim_id"],
            receipt_id=d["receipt_id"],
            player_id=d["player_id"],
            course_id=d["course_id"],
            status=d["status"],
            portions=[Portion.from_dict(p) for p in d["portions"]],
            effects=[ClaimEffect.from_dict(e) for e in d["effects"]],
            totals=dict(d["totals"]),
            supplement_deadline=d["supplement_deadline"],
            follow_up=d["follow_up"],
            follow_up_at=d["follow_up_at"],
            submitted_by=d["submitted_by"],
            submitted_at=d["submitted_at"],
            decided_at=d["decided_at"],
            paid_at=d["paid_at"],
            history=list(d["history"]),
        )


@dataclass
class PaymentBatchLine:
    claim_id: str
    receipt_id: str
    portion_ids: list[str]
    amount: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class PaymentBatch:
    batch_id: str
    policy_id: str
    payer_type: str
    currency: str
    lines: list[PaymentBatchLine]
    total: str
    created_by: str
    created_at: str

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "PaymentBatch":
        return cls(
            batch_id=d["batch_id"],
            policy_id=d["policy_id"],
            payer_type=d["payer_type"],
            currency=d["currency"],
            lines=[PaymentBatchLine(**line) for line in d["lines"]],
            total=d["total"],
            created_by=d["created_by"],
            created_at=d["created_at"],
        )


@dataclass
class Event:
    """审计事件。at 为决定时点；决定类事件（预授权过期、方案变更、
    跨年度切换、跟进标记）都通过事件留下决定时点。"""

    event_id: str
    type: str
    at: str
    actor: str
    entity_kind: str
    entity_id: str
    detail: dict

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Event":
        return cls(**d)
