"""OpenAPI 文档的共享模型与错误响应声明（M3.6 ④）。

原生接口的错误体固定是 ``{"error": {code, message, details, request_id}}``
（见[接口规范 §4]）；把它做成 pydantic 模型后，路由可以用
``responses=errors(401, 403, …)`` 一次性声明，`/docs` 里就能直接看到错误形状，
而不用每个路由手抄一份 dict。

``/v1`` 的错误体不同（``{"error": {message, type, code}}``），由
`modules.chat.openai` 自己维护，不经过这里。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

# 各状态码在文档里的说明：与接口规范 §4 的表格同口径，只写「这一类错误」的含义。
DESCRIPTIONS: dict[int, str] = {
    400: "请求不合法（不支持的媒体类型、地址不允许等）",
    401: "缺少或无效凭证（Key 不存在、已吊销、已过期）",
    403: "没有权限（scope 不足、应用停用、IP 不在白名单）",
    404: "对象不存在（含「不属于本应用」的情况）",
    409: "状态冲突（重复提交、构建中、会话已关闭、转人工状态已变更）",
    413: "上传文件超过 `AICS_UPLOAD_MAX_MB`",
    422: "请求体或参数校验失败，`details.errors` 列出字段",
    429: "超过速率或配额限制，带 `Retry-After`",
    500: "服务内部错误",
    502: "模型供应商返回错误",
    503: "依赖不可用（熔断或维护）",
    504: "上游超时",
}


class ErrorItem(BaseModel):
    """错误体的内层对象。"""

    code: str = Field(description="稳定的业务错误码，接入方按它分支", examples=["VALIDATION_ERROR"])
    message: str = Field(description="给人看的中文说明", examples=["参数校验失败"])
    details: dict[str, Any] = Field(
        default_factory=dict,
        description="结构化补充信息（如 `errors`、`message_id`、`required_scope`）",
    )
    request_id: str | None = Field(default=None, description="与响应头 `X-Request-ID` 一致")


class ErrorEnvelope(BaseModel):
    """原生接口的统一错误体（接口规范 §4）。"""

    error: ErrorItem


def errors(*statuses: int) -> dict[int, dict[str, Any]]:
    """给 ``responses=`` 用的错误响应声明。"""

    return {
        status: {"model": ErrorEnvelope, "description": DESCRIPTIONS.get(status, "错误")}
        for status in statuses
    }


# 应用作用域路由最常出现的四类错误：凭证、权限、对象、参数；限流单独按需追加。
APP_SCOPE_ERRORS = (401, 403, 404, 422)
