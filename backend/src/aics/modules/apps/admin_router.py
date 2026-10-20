"""应用管理接口（M4.2 ②）：列表、详情、配置保存、模型与切块预设。

前缀 `/api/v1/admin`；权限走 `core.security.require_perm`（路径里有 `app_id` 的路由）或
`require_user`（全局字典类接口）。配置的合并、校验与 `config_version` 递增仍由
`AppService.update_app` 负责——这里**不重复**定义 pydantic 模型，请求体按原样交给它，
422 的 `details.errors` 直接给出字段路径（接口参考 §应用）。
"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Body, Depends, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from aics.core.errors import ValidationAppError
from aics.core.openapi import APP_SCOPE_ERRORS, errors
from aics.core.security import AuthedUser, require_perm, require_user
from aics.modules.apps.admin_service import ActorMeta, AppsAdminService
from aics.modules.apps.runtime import AppsAdminRuntime
from aics.modules.apps.service import list_chunk_presets

router = APIRouter(prefix="/api/v1/admin", tags=["admin-apps"])

CurrentUser = Annotated[AuthedUser, Depends(require_user())]
AppReader = require_perm("app.read")
AppWriter = require_perm("app.write")


def _runtime(request: Request) -> AppsAdminRuntime:
    return request.app.state.apps_admin_runtime


async def _service(request: Request) -> Any:
    runtime = _runtime(request)
    async with runtime.session() as session:
        yield await runtime.service(session)


Service = Annotated[AppsAdminService, Depends(_service)]


def _meta(request: Request) -> ActorMeta:
    state = request.scope.get("state") or {}
    client = request.client
    return ActorMeta(
        ip=None if client is None else client.host,
        user_agent=request.headers.get("user-agent"),
        request_id=state.get("request_id"),
    )


@router.get("/apps", summary="应用列表（按成员关系过滤）", responses=errors(401, 403, 422))
async def list_apps(
    request: Request,
    user: CurrentUser,
    service: Service,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> JSONResponse:
    items = await service.list_apps(
        user_id=user.id, is_super_admin=user.is_super_admin, limit=limit, offset=offset
    )
    return JSONResponse(jsonable_encoder({"items": items}))


@router.get(
    "/apps/{app_id}",
    summary="应用详情（配置 + 统计）",
    responses=errors(*APP_SCOPE_ERRORS),
)
async def get_app(
    request: Request,
    app_id: UUID,
    user: Annotated[AuthedUser, Depends(AppReader)],
    service: Service,
) -> JSONResponse:
    return JSONResponse(jsonable_encoder(await service.detail(app_id)))


@router.patch(
    "/apps/{app_id}",
    summary="更新应用（部分更新，可带 expected_config_version 做乐观校验）",
    responses=errors(*APP_SCOPE_ERRORS, 409),
)
async def patch_app(
    request: Request,
    app_id: UUID,
    user: Annotated[AuthedUser, Depends(AppWriter)],
    service: Service,
    body: Annotated[dict[str, Any], Body()],
) -> JSONResponse:
    patch = dict(body)
    expected = patch.pop("expected_config_version", None)
    if expected is not None and not isinstance(expected, int):
        raise ValidationAppError(
            details={
                "errors": [
                    {"loc": ["expected_config_version"], "msg": "应为整数（当前 config_version）"}
                ]
            }
        )
    payload = await service.update(
        app_id, patch, expected_version=expected, actor_id=user.id, meta=_meta(request)
    )
    return JSONResponse(jsonable_encoder(payload))


@router.get("/models", summary="模型注册表（配置页下拉）", responses=errors(401, 422))
async def list_models(request: Request, user: CurrentUser) -> JSONResponse:
    return JSONResponse(jsonable_encoder(_runtime(request).settings.models.public()))


@router.get("/chunk-presets", summary="切块预设（展开后的完整配置）", responses=errors(401, 422))
async def list_presets(request: Request, user: CurrentUser) -> JSONResponse:
    return JSONResponse(jsonable_encoder(list_chunk_presets()))


@router.get(
    "/system/settings",
    summary="平台系统设置（v1 只有默认会话配置，S）",
    responses=errors(401, 403, 422),
)
async def get_system_settings(
    request: Request,
    user: Annotated[AuthedUser, Depends(require_perm("system.read"))],
    service: Service,
) -> JSONResponse:
    return JSONResponse(jsonable_encoder(await service.system_settings()))


@router.patch(
    "/system/settings",
    summary="更新平台系统设置（写审计 system.update，S）",
    responses=errors(401, 403, 422),
)
async def update_system_settings(
    request: Request,
    user: Annotated[AuthedUser, Depends(require_perm("system.write"))],
    service: Service,
    body: Annotated[dict[str, Any], Body()],
) -> JSONResponse:
    payload = await service.update_system_settings(body, actor_id=user.id, meta=_meta(request))
    return JSONResponse(jsonable_encoder(payload))


@router.post(
    "/apps",
    summary="创建应用（默认配置，S）",
    status_code=201,
    responses=errors(401, 403, 422),
)
async def create_app(
    request: Request,
    user: Annotated[AuthedUser, Depends(require_perm("app.create"))],
    service: Service,
    body: Annotated[dict[str, Any], Body()],
) -> JSONResponse:
    payload = await service.create(body, actor_id=user.id, meta=_meta(request))
    return JSONResponse(jsonable_encoder(payload), status_code=201)


@router.post(
    "/apps/{app_id}/copy",
    summary="复制应用（只复制配置与基础信息，S）",
    status_code=201,
    responses=errors(*APP_SCOPE_ERRORS),
)
async def copy_app(
    request: Request,
    app_id: UUID,
    user: Annotated[AuthedUser, Depends(require_perm("app.copy"))],
    service: Service,
) -> JSONResponse:
    payload = await service.copy(app_id, actor_id=user.id, meta=_meta(request))
    return JSONResponse(jsonable_encoder(payload), status_code=201)


@router.delete(
    "/apps/{app_id}",
    summary="删除应用（置 deleting 并投递清理任务，S）",
    status_code=202,
    responses=errors(*APP_SCOPE_ERRORS),
)
async def delete_app(
    request: Request,
    app_id: UUID,
    user: Annotated[AuthedUser, Depends(require_perm("app.delete"))],
    service: Service,
) -> JSONResponse:
    payload = await service.delete(app_id, actor_id=user.id, meta=_meta(request))
    return JSONResponse(jsonable_encoder(payload), status_code=202)
