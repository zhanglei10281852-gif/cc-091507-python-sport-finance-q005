"""基础领域工具：金额、时间、标识与异常。

所有金额在系统内部一律以“分”(整数) 保存，接口输入接受元（字符串或数字），
避免浮点误差；数量按 reference/domain.json 的 quantity_precision 支持 6 位小数。
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

CENT = Decimal("0.01")
UNIT = Decimal("1")
QUANTITY_PRECISION = 6


class ValidationError(Exception):
    """请求数据不合法（HTTP 400）。"""

    def __init__(self, message: str, field: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.field = field


class NotFoundError(Exception):
    """资源不存在（HTTP 404）。"""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class ConflictError(Exception):
    """业务冲突（HTTP 409），payload 携带冲突对象的当前状态。"""

    def __init__(self, message: str, payload: dict) -> None:
        super().__init__(message)
        self.message = message
        self.payload = payload


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(moment: datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_date(value: object, field: str) -> date:
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError):
        raise ValidationError(f"日期格式应为 YYYY-MM-DD：{value!r}", field)


def to_cents(value: object, field: str = "amount") -> int:
    """把元的字符串/数字转换为分（整数），四舍五入到分。"""
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValidationError(f"金额格式不正确：{value!r}", field)
    if amount.is_nan() or amount.is_infinite():
        raise ValidationError(f"金额格式不正确：{value!r}", field)
    if amount < 0:
        raise ValidationError("金额不能为负数", field)
    return int((amount / CENT).to_integral_value(rounding=ROUND_HALF_UP))


def cents_to_str(cents: int) -> str:
    return str((Decimal(int(cents)) * CENT).quantize(CENT))


def parse_quantity(value: object, field: str = "quantity") -> Decimal:
    try:
        quantity = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValidationError(f"数量格式不正确：{value!r}", field)
    if quantity.is_nan() or quantity.is_infinite():
        raise ValidationError(f"数量格式不正确：{value!r}", field)
    if quantity <= 0:
        raise ValidationError("数量必须大于 0", field)
    if -quantity.as_tuple().exponent > QUANTITY_PRECISION:
        raise ValidationError(f"数量最多支持 {QUANTITY_PRECISION} 位小数", field)
    return quantity


def parse_ratio(value: object, field: str = "coverage_ratio") -> Decimal:
    try:
        ratio = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValidationError(f"责任比例格式不正确：{value!r}", field)
    if ratio < 0 or ratio > 1:
        raise ValidationError("责任比例必须在 0 与 1 之间", field)
    return ratio


def require_str(payload: dict, field: str) -> str:
    value = payload.get(field)
    if value is None or str(value).strip() == "":
        raise ValidationError(f"缺少必填字段：{field}", field)
    return str(value)


def optional_str(payload: dict, field: str) -> str | None:
    value = payload.get(field)
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def require_list(payload: dict, field: str) -> list:
    value = payload.get(field)
    if not isinstance(value, list) or not value:
        raise ValidationError(f"字段 {field} 必须是非空数组", field)
    return value


def str_list(payload: dict, field: str) -> list[str]:
    value = payload.get(field, [])
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValidationError(f"字段 {field} 必须是字符串数组", field)
    return list(value)
