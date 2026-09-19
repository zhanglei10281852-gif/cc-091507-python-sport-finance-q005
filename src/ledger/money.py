"""金额与数量的十进制处理。金额统一两位小数（分），数量六位小数。"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from .errors import ValidationError

CENT = Decimal("0.01")
QTY_UNIT = Decimal("0.000001")
ZERO = Decimal("0.00")


def _to_decimal(value, field: str) -> Decimal:
    if isinstance(value, bool):
        raise ValidationError(f"{field} 必须是数字", details={"field": field, "value": value})
    if isinstance(value, Decimal):
        d = value
    elif isinstance(value, int):
        d = Decimal(value)
    elif isinstance(value, float):
        d = Decimal(str(value))
    elif isinstance(value, str):
        try:
            d = Decimal(value.strip())
        except InvalidOperation:
            raise ValidationError(
                f"{field} 不是有效数字: {value!r}", details={"field": field, "value": value}
            )
    else:
        raise ValidationError(f"{field} 必须是数字", details={"field": field})
    if not d.is_finite():
        raise ValidationError(f"{field} 必须是有限数字", details={"field": field})
    return d


def parse_money(value, field: str = "amount") -> Decimal:
    return _to_decimal(value, field).quantize(CENT, rounding=ROUND_HALF_UP)


def parse_quantity(value, field: str = "quantity") -> Decimal:
    return _to_decimal(value, field).quantize(QTY_UNIT, rounding=ROUND_HALF_UP)


def parse_ratio(value, field: str = "ratio") -> Decimal:
    d = _to_decimal(value, field)
    if d < 0 or d > 1:
        raise ValidationError(
            f"{field} 必须介于 0 与 1 之间", details={"field": field, "value": str(d)}
        )
    return d


def money_str(value: Decimal) -> str:
    return str(value.quantize(CENT, rounding=ROUND_HALF_UP))


def qty_str(value: Decimal) -> str:
    return str(value.quantize(QTY_UNIT, rounding=ROUND_HALF_UP))
