"""统一异常与错误响应（docs/04-api/01-接口规范.md §3–4）。"""

from __future__ import annotations

from typing import Any


class AppError(Exception):
    """业务异常基类：抛出它（或子类），由全局处理器转为统一错误响应。"""

    code: str = "INTERNAL_ERROR"
    status: int = 500
    message: str = "服务内部错误"

    def __init__(
        self,
        code: str | None = None,
        message: str | None = None,
        status: int | None = None,
        details: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.code = code or self.code
        self.message = message or self.message
        self.status = status or self.status
        self.details = details or {}
        # Extra response headers (e.g. Retry-After / X-RateLimit-* on 429).
        self.headers = headers or {}
        super().__init__(f"{self.code}: {self.message}")


class NotFoundError(AppError):
    code, status, message = "NOT_FOUND", 404, "资源不存在"


class ConversationNotFoundError(NotFoundError):
    """Conversation missing or not owned by the app (open API: 404 CONVERSATION_NOT_FOUND)."""

    code, message = "CONVERSATION_NOT_FOUND", "会话不存在"


class UnauthenticatedError(AppError):
    code, status, message = "UNAUTHENTICATED", 401, "缺少或无效的凭证"


class ForbiddenError(AppError):
    code, status, message = "FORBIDDEN", 403, "权限不足"


class AppNotFoundError(NotFoundError):
    code, message = "APP_NOT_FOUND", "应用不存在"


class ConflictError(AppError):
    code, status, message = "CONFLICT", 409, "资源状态冲突"


class ValidationAppError(AppError):
    code, status, message = "VALIDATION_ERROR", 422, "参数校验失败"


def error_body(
    code: str, message: str, request_id: str | None, details: dict[str, Any] | None = None
) -> dict[str, Any]:
    return {
        "error": {
            "code": code,
            "message": message,
            "details": details or {},
            "request_id": request_id,
        }
    }
