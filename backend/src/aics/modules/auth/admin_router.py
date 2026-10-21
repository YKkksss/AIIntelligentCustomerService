"""管理后台认证路由（/api/v1/admin/auth/*，管理接口参考 §认证）。

这里只做协议转换：取 runtime、拼 `RequestMeta`、把 service 的返回值序列化成 JSON。
权限判定在 `core.security.require_user`（只认 JWT），与开放接口的 API Key 认证完全两套。
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import JSONResponse

from aics.core.openapi import errors
from aics.core.security import AuthedUser, require_perm, require_user
from aics.modules.auth.runtime import AuthRuntime
from aics.modules.auth.schemas import (
    ChangePasswordBody,
    LoginBody,
    LogoutBody,
    MemberBody,
    RefreshBody,
    UserCreateBody,
    UserPatchBody,
)
from aics.modules.auth.service import RequestMeta

router = APIRouter(prefix="/api/v1/admin", tags=["admin-auth"])

CurrentUser = Annotated[AuthedUser, Depends(require_user())]
UserAdmin = Annotated[AuthedUser, Depends(require_perm("user.admin"))]
MemberReader = Annotated[AuthedUser, Depends(require_perm("app.read"))]
MemberWriter = Annotated[AuthedUser, Depends(require_perm("member.write"))]


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
            body.email, body.password, remember_days=body.remember_days, meta=_meta(request)
        )
    return JSONResponse(result.model_dump(mode="json"))


@router.post(
    "/auth/refresh",
    summary="用 refresh token 换新的一对 token（旧的立即失效；同时返回最新用户与可见应用）",
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


# ----------------------------------------------------------- 用户与成员（M4.1 ③）
@router.get(
    "/users",
    summary="用户列表（可按邮箱 / 姓名模糊搜索）",
    responses=errors(401, 403, 422),
)
async def list_users(
    request: Request,
    user: UserAdmin,
    q: Annotated[str | None, Query(max_length=100)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> JSONResponse:
    runtime = _runtime(request)
    async with runtime.sessions() as session:
        items = await runtime.service(session).users(q=q, limit=limit)
    return JSONResponse({"items": [item.model_dump(mode="json") for item in items]})


@router.post(
    "/users",
    status_code=201,
    summary="新建后台用户（首次登录需改密）",
    responses=errors(401, 403, 409, 422),
)
async def create_user(body: UserCreateBody, request: Request, user: UserAdmin) -> JSONResponse:
    runtime = _runtime(request)
    async with runtime.sessions() as session:
        created = await runtime.service(session).create_user(
            email=body.email,
            display_name=body.display_name,
            password=body.password,
            is_super_admin=body.is_super_admin,
            actor_id=user.id,
            meta=_meta(request),
        )
    return JSONResponse(created.model_dump(mode="json"), status_code=201)


@router.patch(
    "/users/{user_id}",
    summary="修改用户（姓名 / 状态 / 超管 / 重置密码；改密或禁用会踢掉旧 token）",
    responses=errors(401, 403, 404, 422),
)
async def patch_user(
    user_id: UUID, body: UserPatchBody, request: Request, user: UserAdmin
) -> JSONResponse:
    runtime = _runtime(request)
    async with runtime.sessions() as session:
        updated = await runtime.service(session).update_user(
            user_id,
            display_name=body.display_name,
            status=body.status,
            is_super_admin=body.is_super_admin,
            password=body.password,
            actor_id=user.id,
            meta=_meta(request),
        )
    return JSONResponse(updated.model_dump(mode="json"))


@router.delete(
    "/users/{user_id}",
    status_code=204,
    summary="删除用户（软删除，立即失效其 token）",
    responses=errors(401, 403, 404, 422),
)
async def delete_user(user_id: UUID, request: Request, user: UserAdmin) -> Response:
    runtime = _runtime(request)
    async with runtime.sessions() as session:
        await runtime.service(session).delete_user(user_id, actor_id=user.id, meta=_meta(request))
    return Response(status_code=204)


@router.post(
    "/users/{user_id}/reset-password",
    summary="重置用户密码（返回一次性临时密码，用户首登须改密）",
    responses=errors(401, 403, 404, 422),
)
async def reset_password(user_id: UUID, request: Request, user: UserAdmin) -> JSONResponse:
    runtime = _runtime(request)
    async with runtime.sessions() as session:
        temporary = await runtime.service(session).reset_password(
            user_id, actor_id=user.id, meta=_meta(request)
        )
    return JSONResponse({"password": temporary})


@router.get(
    "/apps/{app_id}/members",
    summary="应用成员（用户 + 角色）",
    responses=errors(401, 403, 404, 422),
)
async def list_members(app_id: UUID, request: Request, user: MemberReader) -> JSONResponse:
    runtime = _runtime(request)
    async with runtime.sessions() as session:
        items = await runtime.service(session).members(app_id)
    return JSONResponse({"items": [item.model_dump(mode="json") for item in items]})


@router.put(
    "/apps/{app_id}/members/{user_id}",
    summary="新增或改派应用成员角色（撤销后立即生效）",
    responses=errors(401, 403, 404, 422),
)
async def put_member(
    app_id: UUID, user_id: UUID, body: MemberBody, request: Request, user: MemberWriter
) -> JSONResponse:
    runtime = _runtime(request)
    async with runtime.sessions() as session:
        row = await runtime.service(session).set_member(
            app_id, user_id, role_id=body.role_id, actor_id=user.id, meta=_meta(request)
        )
    return JSONResponse(row.model_dump(mode="json"))


@router.delete(
    "/apps/{app_id}/members/{user_id}",
    status_code=204,
    summary="移除应用成员（立即失效其权限缓存）",
    responses=errors(401, 403, 404, 422),
)
async def delete_member(
    app_id: UUID, user_id: UUID, request: Request, user: MemberWriter
) -> Response:
    runtime = _runtime(request)
    async with runtime.sessions() as session:
        await runtime.service(session).remove_member(
            app_id, user_id, actor_id=user.id, meta=_meta(request)
        )
    return Response(status_code=204)
