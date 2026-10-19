"""管理后台认证路由（/api/v1/admin/auth/*，管理接口参考 §认证）。

这里只做协议转换：取 runtime、拼 `RequestMeta`、把 service 的返回值序列化成 JSON。
权限判定在 `core.security.require_user`（只认 JWT），与开放接口的 API Key 认证完全两套。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse

from aics.core.openapi import errors
from aics.core.security import AuthedUser, require_user
from aics.modules.auth.runtime import AuthRuntime
from aics.modules.auth.schemas import (
    ChangePasswordBody,
    LoginBody,
    LogoutBody,
    RefreshBody,
)
from aics.modules.auth.service import RequestMeta

router = APIRouter(prefix="/api/v1/admin", tags=["admin-auth"])

CurrentUser = Annotated[AuthedUser, Depends(require_user())]


def _runtime(request: Request) -> AuthRuntime:
    runtime: AuthRuntime | None = getattr(request.app.state, "auth_runtime", None)
    if runtime is None:  # pragma: no cover - wiring error
        raise RuntimeError("auth_runtime is not configured")
    return runtime


def _meta(request: Request) -> RequestMeta:
    state = request.scope.get("state") or {}
    return RequestMeta(
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
        request_id=state.get("request_id"),
    )


@router.post(
    "/auth/login",
    summary="登录（邮箱 + 密码，返回 access / refresh 与可见应用）",
    responses=errors(401, 422, 423, 429),
)
async def login(body: LoginBody, request: Request) -> JSONResponse:
    runtime = _runtime(request)
    async with runtime.sessions() as session:
        result = await runtime.service(session).login(
            body.email, body.password, meta=_meta(request)
        )
    return JSONResponse(result.model_dump(mode="json"))


@router.post(
    "/auth/refresh",
    summary="用 refresh token 换新的一对 token（旧的立即失效）",
    responses=errors(401, 422, 429),
)
async def refresh(body: RefreshBody, request: Request) -> JSONResponse:
    runtime = _runtime(request)
    async with runtime.sessions() as session:
        result = await runtime.service(session).refresh(body.refresh_token)
    return JSONResponse(result.model_dump(mode="json"))


@router.post(
    "/auth/logout",
    status_code=204,
    summary="登出（撤销 refresh token；access 到期自然失效）",
    responses=errors(422),
)
async def logout(body: LogoutBody, request: Request) -> Response:
    runtime = _runtime(request)
    async with runtime.sessions() as session:
        await runtime.service(session).logout(body.refresh_token)
    return Response(status_code=204)


@router.get("/auth/me", summary="当前登录用户与可见应用（含权限名）", responses=errors(401))
async def me(request: Request, user: CurrentUser) -> JSONResponse:
    runtime = _runtime(request)
    async with runtime.sessions() as session:
        result = await runtime.service(session).me(user.id)
    return JSONResponse(result.model_dump(mode="json"))


@router.post(
    "/auth/password",
    summary="修改自己的密码（成功后旧 token 全部失效，返回新的一对）",
    responses=errors(401, 422),
)
async def change_password(
    body: ChangePasswordBody, request: Request, user: CurrentUser
) -> JSONResponse:
    runtime = _runtime(request)
    async with runtime.sessions() as session:
        result = await runtime.service(session).change_password(
            user.id, current=body.current_password, new=body.new_password, meta=_meta(request)
        )
    return JSONResponse(result.model_dump(mode="json"))
