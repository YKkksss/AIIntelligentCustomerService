"""公共知识库的管理接口（M4.7 ③）。

前缀 `/api/v1/admin`：库是平台级资源，所以路由里没有 `app_id`；与客服的关联单独有两条
`/apps/{app_id}/knowledge-bases`。权限：读 `kb.read`、写 `kb.write`（不区分应用，库是公共的）。

文档与切块仍走 `/admin/apps/{app_id}/…`（默认库），本步骤不搬旧路径。
"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from aics.core.openapi import APP_SCOPE_ERRORS, errors
from aics.core.security import AuthedUser, require_perm
from aics.modules.knowledge.kb_service import ActorMeta, KnowledgeBaseService
from aics.modules.knowledge.runtime import KnowledgeRuntime

router = APIRouter(prefix="/api/v1/admin", tags=["admin-knowledge-bases"])

Reader = require_perm("kb.read")
Writer = require_perm("kb.write")

_READ_ERRORS = errors(*APP_SCOPE_ERRORS)
_WRITE_ERRORS = errors(401, 403, 404, 409, 422, 429)


def _runtime(request: Request) -> KnowledgeRuntime:
    return request.app.state.knowledge_runtime


async def _service(request: Request) -> Any:
    runtime = _runtime(request)
    async with runtime.session() as session:
        yield runtime.kb_service(session)


Service = Annotated[KnowledgeBaseService, Depends(_service)]


def _meta(request: Request) -> ActorMeta:
    state = request.scope.get("state") or {}
    client = request.client
    return ActorMeta(
        ip=None if client is None else client.host,
        user_agent=request.headers.get("user-agent"),
        request_id=state.get("request_id"),
    )


class KBAppLink(BaseModel):
    app_id: str
    name: str


class KBRow(BaseModel):
    id: str
    name: str
    description: str
    status: str
    chunk_config: dict[str, Any]
    config_version: int
    document_count: int
    chunk_count: int
    apps: list[KBAppLink] = Field(default_factory=list)
    linked: bool | None = None
    is_default: bool | None = None
    created_at: Any
    updated_at: Any


class KBPage(BaseModel):
    items: list[KBRow]
    total: int
    page: int
    page_size: int


class KBLinkPage(BaseModel):
    items: list[KBRow]


class KBCreate(BaseModel):
    model_config = {"extra": "forbid"}

    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=500)
    chunk_config: dict[str, Any] | None = None


class KBPatch(BaseModel):
    model_config = {"extra": "forbid"}

    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=500)
    chunk_config: dict[str, Any] | None = None
    status: str | None = None


class KBLinkBody(BaseModel):
    model_config = {"extra": "forbid"}

    kb_ids: list[UUID] = Field(default_factory=list, max_length=50)
    #: 旧接口落到哪个库；不传时保留原默认（没有则取第一个）
    default_kb_id: UUID | None = None


@router.get("/knowledge-bases", summary="知识库列表（平台公共）", responses=_READ_ERRORS)
async def list_knowledge_bases(
    request: Request,
    user: Annotated[AuthedUser, Depends(Reader)],
    service: Service,
    status: Annotated[str | None, Query(pattern="^(active|disabled|deleting)$")] = None,
    q: Annotated[str | None, Query(max_length=100)] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> JSONResponse:
    items, total = await service.list(status=status, q=q, page=page, page_size=page_size)
    body = {"items": items, "total": total, "page": page, "page_size": page_size}
    return JSONResponse(jsonable_encoder(body))


@router.post(
    "/knowledge-bases",
    status_code=201,
    summary="新建知识库",
    responses=_WRITE_ERRORS,
    response_model=KBRow,
)
async def create_knowledge_base(
    request: Request,
    body: KBCreate,
    user: Annotated[AuthedUser, Depends(Writer)],
    service: Service,
) -> dict[str, Any]:
    return await service.create(
        name=body.name,
        description=body.description,
        chunk_config=body.chunk_config,
        actor_id=user.id,
        meta=_meta(request),
    )


@router.get(
    "/knowledge-bases/{kb_id}",
    summary="知识库详情",
    responses=_READ_ERRORS,
    response_model=KBRow,
)
async def get_knowledge_base(
    kb_id: UUID,
    user: Annotated[AuthedUser, Depends(Reader)],
    service: Service,
) -> dict[str, Any]:
    return await service.detail(kb_id)


@router.patch(
    "/knowledge-bases/{kb_id}",
    summary="修改知识库",
    responses=_WRITE_ERRORS,
    response_model=KBRow,
)
async def patch_knowledge_base(
    request: Request,
    kb_id: UUID,
    body: KBPatch,
    user: Annotated[AuthedUser, Depends(Writer)],
    service: Service,
) -> dict[str, Any]:
    patch = body.model_dump(exclude_unset=True)
    return await service.update(kb_id, patch, actor_id=user.id, meta=_meta(request))


@router.delete(
    "/knowledge-bases/{kb_id}", status_code=204, summary="删除知识库", responses=_WRITE_ERRORS
)
async def delete_knowledge_base(
    request: Request,
    kb_id: UUID,
    user: Annotated[AuthedUser, Depends(Writer)],
    service: Service,
) -> Response:
    await service.delete(kb_id, actor_id=user.id, meta=_meta(request))
    return Response(status_code=204)


@router.get(
    "/apps/{app_id}/knowledge-bases",
    summary="客服关联的知识库",
    responses=_READ_ERRORS,
    response_model=KBLinkPage,
)
async def list_app_knowledge_bases(
    app_id: UUID,
    user: Annotated[AuthedUser, Depends(Reader)],
    service: Service,
) -> dict[str, Any]:
    return {"items": await service.links_of(app_id)}


@router.put(
    "/apps/{app_id}/knowledge-bases",
    summary="设置客服关联的知识库",
    responses=_WRITE_ERRORS,
    response_model=KBLinkPage,
)
async def put_app_knowledge_bases(
    request: Request,
    app_id: UUID,
    body: KBLinkBody,
    user: Annotated[AuthedUser, Depends(Writer)],
    service: Service,
) -> dict[str, Any]:
    items = await service.set_links(
        app_id,
        body.kb_ids,
        default_kb_id=body.default_kb_id,
        actor_id=user.id,
        meta=_meta(request),
    )
    return {"items": items}
