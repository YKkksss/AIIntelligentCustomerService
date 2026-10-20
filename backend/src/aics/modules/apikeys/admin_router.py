"""API Key 管理接口（M4.5 ②）。

前缀 `/api/v1/admin/apps/{app_id}`，权限 `key.read` / `key.write`（`require_perm` 从路径里取
`app_id` 判成员关系）：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/keys` | 列表（脱敏，含 `last_used_at` 与状态） |
| POST | `/keys` | 创建，**明文只在这次响应里出现** |
| PATCH | `/keys/{key_id}` | 改 `rpm_limit` / `daily_quota` / `ip_allowlist`，改完删缓存 |
| DELETE | `/keys/{key_id}` | 吊销并删缓存（立即 401） |
| GET | `/keys/{key_id}/example` | curl / Python / JavaScript 接入示例（Key 用占位符） |

响应都带 `response_model`（M4.5 起新接口一律如此，见 ISSUE-86）：前端类型可由
`openapi-typescript` 生成，不必再手写。写操作审计 `key.create` / `key.update` / `key.revoke`，
与业务写在**同一条事务**里。
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field

from aics.core.openapi import APP_SCOPE_ERRORS, errors
from aics.core.security import AuthedUser, require_perm
from aics.modules.apikeys.admin_runtime import ApiKeyAdminRuntime
from aics.modules.apikeys.admin_service import KEEP, ActorMeta
from aics.modules.apikeys.service import DEFAULT_RPM_LIMIT, NAME_MAX_CHARS

router = APIRouter(prefix="/api/v1/admin/apps/{app_id}", tags=["admin-keys"])

KeyReader = require_perm("key.read")
KeyWriter = require_perm("key.write")

KEY_STATUS = Literal["active", "expired", "revoked"]


class KeyRow(BaseModel):
    """One key, never containing the secret or its hash."""

    id: UUID
    name: str
    prefix: str
    hint: str
    scopes: list[str]
    rpm_limit: int
    daily_quota: int | None = None
    ip_allowlist: list[str] = Field(default_factory=list)
    last_used_at: datetime | None = None
    expires_at: datetime | None = None
    revoked_at: datetime | None = None
    created_at: datetime
    status: KEY_STATUS


class KeyPage(BaseModel):
    items: list[KeyRow]


class CreatedKey(KeyRow):
    """`key` 是明文，只在创建时返回一次；服务端只存 HMAC 与前缀。"""

    key: str


class KeyCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=NAME_MAX_CHARS)
    scopes: list[str] = Field(min_length=1)
    rpm_limit: int = Field(default=DEFAULT_RPM_LIMIT, ge=1)
    daily_quota: int | None = Field(default=None, ge=1)
    expires_at: datetime | None = None
    ip_allowlist: list[str] | None = None


class KeyPatch(BaseModel):
    """只提交要改的键；`daily_quota: null` 表示取消配额，`ip_allowlist: null` 表示不限制。"""

    model_config = ConfigDict(extra="forbid")

    rpm_limit: int | None = Field(default=None, ge=1)
    daily_quota: int | None = Field(default=None, ge=1)
    ip_allowlist: list[str] | None = None


class KeyExample(BaseModel):
    key_id: UUID
    name: str
    curl: str
    python: str
    javascript: str
    url: str


def _runtime(request: Request) -> ApiKeyAdminRuntime:
    return request.app.state.apikeys_admin_runtime


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
    "/keys",
    summary="API Key 列表（脱敏，含最后使用时间）",
    response_model=KeyPage,
    responses=errors(*APP_SCOPE_ERRORS),
)
async def list_keys(
    request: Request,
    app_id: UUID,
    user: Annotated[AuthedUser, Depends(KeyReader)],
    service: Service,
) -> dict[str, Any]:
    return {"items": await service.list_keys(app_id)}


@router.post(
    "/keys",
    summary="创建 API Key（明文只返回一次）",
    status_code=201,
    response_model=CreatedKey,
    responses=errors(*APP_SCOPE_ERRORS, 429),
)
async def create_key(
    request: Request,
    app_id: UUID,
    body: KeyCreate,
    user: Annotated[AuthedUser, Depends(KeyWriter)],
    service: Service,
) -> dict[str, Any]:
    return await service.create_key(
        app_id,
        name=body.name,
        scopes=body.scopes,
        rpm_limit=body.rpm_limit,
        daily_quota=body.daily_quota,
        expires_at=body.expires_at,
        ip_allowlist=body.ip_allowlist,
        actor_id=user.id,
        meta=_meta(request),
    )


@router.patch(
    "/keys/{key_id}",
    summary="修改限额 / 配额 / IP 白名单（立即生效）",
    response_model=KeyRow,
    responses=errors(*APP_SCOPE_ERRORS),
)
async def patch_key(
    request: Request,
    app_id: UUID,
    key_id: UUID,
    body: KeyPatch,
    user: Annotated[AuthedUser, Depends(KeyWriter)],
    service: Service,
) -> dict[str, Any]:
    provided = body.model_fields_set
    return await service.update_key(
        app_id,
        key_id,
        rpm_limit=body.rpm_limit,
        daily_quota=body.daily_quota if "daily_quota" in provided else KEEP,
        ip_allowlist=body.ip_allowlist if "ip_allowlist" in provided else KEEP,
        actor_id=user.id,
        meta=_meta(request),
    )


@router.delete(
    "/keys/{key_id}",
    summary="吊销 API Key（删缓存，立即 401）",
    response_model=KeyRow,
    responses=errors(*APP_SCOPE_ERRORS),
)
async def revoke_key(
    request: Request,
    app_id: UUID,
    key_id: UUID,
    user: Annotated[AuthedUser, Depends(KeyWriter)],
    service: Service,
) -> dict[str, Any]:
    return await service.revoke_key(app_id, key_id, actor_id=user.id, meta=_meta(request))


@router.get(
    "/keys/{key_id}/example",
    summary="接入示例（curl / Python / JavaScript）",
    response_model=KeyExample,
    responses=errors(*APP_SCOPE_ERRORS),
)
async def key_example(
    request: Request,
    app_id: UUID,
    key_id: UUID,
    user: Annotated[AuthedUser, Depends(KeyReader)],
    service: Service,
) -> dict[str, Any]:
    base = str(request.base_url).rstrip("/")
    return await service.example(app_id, key_id, base_url=base)


__all__ = ["router", "KeyRow", "KeyPage", "CreatedKey", "KeyExample", "KeyCreate", "KeyPatch"]
