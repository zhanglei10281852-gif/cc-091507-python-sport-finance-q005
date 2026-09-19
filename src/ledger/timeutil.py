"""日期与时间戳处理。日期为 YYYY-MM-DD；时间戳为 UTC ISO 8601。"""
from __future__ import annotations

from datetime import date, datetime, timezone

from .errors import ValidationError


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def parse_date(value, field: str) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{field} 必须是 YYYY-MM-DD 字符串", details={"field": field})
    try:
        d = date.fromisoformat(value.strip())
    except ValueError:
        raise ValidationError(
            f"{field} 必须是 YYYY-MM-DD: {value!r}", details={"field": field, "value": value}
        )
    return d.isoformat()


def parse_datetime(value, field: str) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{field} 必须是 ISO 8601 时间戳", details={"field": field})
    text = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        raise ValidationError(
            f"{field} 不是有效时间戳: {value!r}", details={"field": field, "value": value}
        )
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return to_iso(dt)


def to_dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
