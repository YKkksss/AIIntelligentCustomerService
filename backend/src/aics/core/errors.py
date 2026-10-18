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


class DocumentNotFoundError(NotFoundError):
    """Document missing or not owned by the app (open API: 404 DOCUMENT_NOT_FOUND).

    放在 core 而不是 `modules.knowledge`：入库流水线（ingest，L2 之下）也要抛同一个错误码，
    而 ingest 不允许反向 import 模块层（import-linter 层次合同）。
    """

    code, message = "DOCUMENT_NOT_FOUND", "文档不存在"


class UnauthenticatedError(AppError):
    code, status, message = "UNAUTHENTICATED", 401, "缺少或无效的凭证"


class ForbiddenError(AppError):
    code, status, message = "FORBIDDEN", 403, "权限不足"


class AppNotFoundError(NotFoundError):
    code, message = "APP_NOT_FOUND", "应用不存在"


class ConflictError(AppError):
    code, status, message = "CONFLICT", 409, "资源状态冲突"


class HandoffStateConflictError(ConflictError):
    """转人工状态迁移被抢先（M3.4 ②）：`details.handoff_status` 是数据库里的当前值。

    条件单行 UPDATE 返回 0 行时抛出——坐席接口（开放接口参考 §5）与队列超时/空闲回收
    （会话生命周期与转人工 §3.6、§3.8）都用它表示「状态已被别人改掉」。"""

    code, message = "HANDOFF_STATE_CONFLICT", "转人工状态已变更"


class FileTooLargeError(AppError):
    """上传超过 `AICS_UPLOAD_MAX_MB`（接口规范 §4：413 `FILE_TOO_LARGE`）。"""

    code, status, message = "FILE_TOO_LARGE", 413, "文件超过大小上限"


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
