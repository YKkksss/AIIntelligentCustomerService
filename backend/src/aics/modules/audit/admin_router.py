"""审计查询接口：`GET /api/v1/admin/audit`。

平台级查询（不带 `app_id`）只有 super_admin 能看；带 `app_id` 时按应用授权判定
（`audit.read`）。之所以不用 `require_perm` 依赖：路由路径里没有 `app_id`，应用来自查询参数，
依赖注入拿不到——这里用 `require_user()` + 显式 `authorizer.authorize(...)`。
"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from aics.core.openapi import APP_SCOPE_ERRORS, errors
from aics.core.security import (
    AuthedUser,
    Authorizer,
    ensure_password_changed,
    require_user,
)

router = APIRouter(prefix="/api/v1/admin", tags=["admin-audit"])

CurrentUser = Annotated[AuthedUser, Depends(require_user())]


def _runtime(request: Request) -> Any:
    return request.app.state.audit_runtime


def _authorizer(request: Request) -> Authorizer:
    return request.app.state.authorizer


@router.get(
    "/audit",
    summary="审计日志（id 降序游标分页）",
    responses=errors(*APP_SCOPE_ERRORS),
)
async def list_audit(
    request: Request,
    user: CurrentUser,
    app_id: Annotated[UUID | None, Query()] = None,
    action: Annotated[str | None, Query(max_length=100)] = None,
    actor_id: Annotated[UUID | None, Query()] = None,
    cursor: Annotated[str | None, Query(max_length=200)] = None,
    page_size: Annotated[int, Query(ge=1, le=200)] = 50,
) -> JSONResponse:
    ensure_password_changed(user)
    await _authorizer(request).authorize(user, app_id=app_id, permission="audit.read")
    async with _runtime(request).session() as session:
        payload = (
            await _runtime(request)
            .service(session)
            .page(
                app_id=app_id,
                action=action,
                actor_id=actor_id,
                cursor=cursor,
                page_size=page_size,
            )
        )
    return JSONResponse(jsonable_encoder(payload))


@router.get(
    "/audit/actions",
    summary="审计里出现过的动作名（筛选用）",
    responses=errors(*APP_SCOPE_ERRORS),
)
async def list_actions(request: Request, user: CurrentUser) -> JSONResponse:
    ensure_password_changed(user)
    await _authorizer(request).authorize(user, app_id=None, permission="audit.read")
    async with _runtime(request).session() as session:
        return JSONResponse({"items": await _runtime(request).service(session).actions()})
