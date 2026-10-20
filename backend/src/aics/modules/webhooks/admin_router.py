"""Webhook 管理接口（M4.5 ④）。

前缀 `/api/v1/admin/apps/{app_id}`，权限 `app.read`（看配置与投递记录）/ `webhook.write`
（重发、发送测试、轮换密钥）。`require_perm` 从路径里取 `app_id` 判定成员关系。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/webhook` | 当前配置：`url` / `secret_prefix` / `ready`（不含密文） |
| GET | `/webhook-deliveries` | 投递记录（`status` / `limit` / `before` 游标），不含 payload |
| POST | `/webhook-deliveries/{delivery_id}/redeliver` | 人工重发（`failed` / `delivered` → `pending`） |
| POST | `/webhook/test` | 同步发一条 `ping`（5 s 超时），返回状态码与耗时 |
| POST | `/webhook/secret` | 生成 / 轮换签名密钥，**明文只返回一次** |

响应都带 `response_model`（M4.5 起新接口一律如此，见 ISSUE-86）。审计 `webhook.test` /
`webhook.redeliver` / `webhook.secret` 与业务写同事务。
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from aics.core.openapi import APP_SCOPE_ERRORS, errors
from aics.core.security import AuthedUser, require_perm
from aics.modules.webhooks.admin_runtime import WebhookAdminRuntime
from aics.modules.webhooks.admin_service import DEFAULT_LIMIT, MAX_LIMIT, ActorMeta

router = APIRouter(prefix="/api/v1/admin/apps/{app_id}", tags=["admin-webhooks"])

WebhookReader = require_perm("app.read")
WebhookWriter = require_perm("webhook.write")

DELIVERY_STATUS = Literal["pending", "delivered", "failed"]
_ERRORS = errors(*APP_SCOPE_ERRORS)
_WRITE_ERRORS = errors(*APP_SCOPE_ERRORS, 429)


class WebhookBody(BaseModel):
    """应用当前的 Webhook 配置；`secret_prefix` 是密钥前缀（明文从不返回）。"""

    url: str | None = None
    secret_prefix: str | None = None
    ready: bool


class DeliveryRow(BaseModel):
    """一条投递记录。`payload` 不回（可能含客户数据），只有状态与错误摘要。"""

    id: UUID
    event: str
    seq: int
    conversation_id: UUID | None = None
    status: DELIVERY_STATUS
    attempts: int
    status_code: int | None = None
    error: str | None = None
    next_attempt_at: datetime | None = None
    delivered_at: datetime | None = None
    created_at: datetime


class DeliveryPage(BaseModel):
    items: list[DeliveryRow]


class TestResult(BaseModel):
    ok: bool
    status_code: int | None = None
    error: str | None = None
    elapsed_ms: int


class SecretBody(BaseModel):
    """`secret` 是 `whsec_` 开头的明文，只在这里返回一次。"""

    model_config = ConfigDict(json_schema_extra={"writeOnly": True})

    secret: str = Field(description="签名密钥明文，只返回一次")
    secret_prefix: str


def _runtime(request: Request) -> WebhookAdminRuntime:
    return request.app.state.webhooks_admin_runtime


async def _service(request: Request) -> Any:
    runtime = _runtime(request)
    async with runtime.session() as session:
        yield runtime.service(session)


Service = Annotated[Any, Depends(_service)]


def _meta(request: Request) -> ActorMeta:
    state = request.scope.get("state") or {}
    client = request.client
    return ActorMeta(
        ip=None if client is None else client.host,
        user_agent=request.headers.get("user-agent"),
        request_id=state.get("request_id"),
    )


@router.get(
    "/webhook",
    summary="读取 Webhook 配置（不含密钥明文）",
    response_model=WebhookBody,
    responses=_ERRORS,
)
async def get_webhook(
    request: Request,
    app_id: UUID,
    user: Annotated[AuthedUser, Depends(WebhookReader)],
    service: Service,
) -> dict[str, Any]:
    return await service.config(app_id)


@router.get(
    "/webhook-deliveries",
    summary="Webhook 投递记录（不含 payload）",
    response_model=DeliveryPage,
    responses=_ERRORS,
)
async def list_deliveries(
    request: Request,
    app_id: UUID,
    user: Annotated[AuthedUser, Depends(WebhookReader)],
    service: Service,
    status: Annotated[DELIVERY_STATUS | None, Query(description="按状态过滤")] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT, description="条数")] = DEFAULT_LIMIT,
    before: Annotated[datetime | None, Query(description="只取该时间之前的记录")] = None,
) -> dict[str, Any]:
    items = await service.deliveries(app_id, status=status, limit=limit, before=before)
    return {"items": items}


@router.post(
    "/webhook-deliveries/{delivery_id}/redeliver",
    summary="人工重发一条投递记录",
    response_model=DeliveryRow,
    responses=_WRITE_ERRORS,
)
async def redeliver(
    request: Request,
    app_id: UUID,
    delivery_id: UUID,
    user: Annotated[AuthedUser, Depends(WebhookWriter)],
    service: Service,
) -> dict[str, Any]:
    return await service.redeliver(app_id, delivery_id, actor_id=user.id, meta=_meta(request))


@router.post(
    "/webhook/test",
    summary="发送一条测试投递（同步，5 秒超时）",
    response_model=TestResult,
    responses=_WRITE_ERRORS,
)
async def send_test(
    request: Request,
    app_id: UUID,
    user: Annotated[AuthedUser, Depends(WebhookWriter)],
    service: Service,
) -> dict[str, Any]:
    return await service.test(app_id, actor_id=user.id, meta=_meta(request))


@router.post(
    "/webhook/secret",
    summary="生成或轮换 Webhook 签名密钥（明文只返回一次）",
    response_model=SecretBody,
    responses=_WRITE_ERRORS,
)
async def rotate_secret(
    request: Request,
    app_id: UUID,
    user: Annotated[AuthedUser, Depends(WebhookWriter)],
    service: Service,
) -> dict[str, Any]:
    return await service.rotate_secret(app_id, actor_id=user.id, meta=_meta(request))
