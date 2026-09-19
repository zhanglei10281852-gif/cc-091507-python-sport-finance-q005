"""账本领域错误类型，HTTP 层按 status 映射为 JSON 错误响应。"""
from __future__ import annotations


class DomainError(Exception):
    status = 400
    code = "domain_error"

    def __init__(self, message: str, *, code: str | None = None, details: dict | None = None):
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        self.details = details or {}

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "details": self.details}


class ValidationError(DomainError):
    status = 400
    code = "validation_error"


class NotFoundError(DomainError):
    status = 404
    code = "not_found"


class ConflictError(DomainError):
    status = 409
    code = "conflict"
