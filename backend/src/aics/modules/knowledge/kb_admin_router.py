"""公共知识库的管理接口（M4.7 ③）。

前缀 `/api/v1/admin`：库是平台级资源，所以路由里没有 `app_id`；与客服的关联单独有两条
`/apps/{app_id}/knowledge-bases`。权限：读 `kb.read`、写 `kb.write`（不区分应用，库是公共的）。

文档与切块仍走 `/admin/apps/{app_id}/…`（默认库），本步骤不搬旧路径。
"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from aics.core.errors import ValidationAppError
from aics.core.openapi import APP_SCOPE_ERRORS, errors
from aics.core.security import AuthedUser, require_perm
from aics.modules.knowledge.kb_docs import ActorMeta as DocActorMeta
from aics.modules.knowledge.kb_docs import KbUploadResult, KnowledgeBaseDocs
from aics.modules.knowledge.kb_service import ActorMeta, KnowledgeBaseService
from aics.modules.knowledge.runtime import KnowledgeRuntime
from aics.storage import get_storage

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
    created_by: str | None = None
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


class KBDocumentRow(BaseModel):
    id: str
    title: str
    status: str
    source_type: str
    chunk_count: int
    # 公共库的文档没有 app_id / app_name（归属看 kb_id）
    app_id: str | None = None
    app_name: str | None = None
    effective_at: Any
    expires_at: Any
    created_at: Any
    updated_at: Any


class KBDocumentPage(BaseModel):
    items: list[KBDocumentRow]
    total: int
    page: int
    page_size: int


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


@router.get(
    "/knowledge-bases/{kb_id}/documents",
    summary="知识库里的文档（平台公共页）",
    responses=_READ_ERRORS,
    response_model=KBDocumentPage,
)
async def list_kb_documents(
    kb_id: UUID,
    user: Annotated[AuthedUser, Depends(Reader)],
    service: Service,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> JSONResponse:
    # 文档仍带 app_id（上传仍走客服页），这里返回 app_name 让运营知道出处
    items, total = await service.documents(kb_id, page=page, page_size=page_size)
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
    return await service.update(
        kb_id, patch, actor_id=user.id, is_super_admin=user.is_super_admin, meta=_meta(request)
    )


@router.delete(
    "/knowledge-bases/{kb_id}", status_code=204, summary="删除知识库", responses=_WRITE_ERRORS
)
async def delete_knowledge_base(
    request: Request,
    kb_id: UUID,
    user: Annotated[AuthedUser, Depends(Writer)],
    service: Service,
) -> Response:
    await service.delete(
        kb_id, actor_id=user.id, is_super_admin=user.is_super_admin, meta=_meta(request)
    )
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


# ---------------------------------------------------------------- 库内文档（M4.7 ⑤）
#: 文档列表/上传等接口的错误声明（写操作多 413/429）
_DOC_READ = errors(*APP_SCOPE_ERRORS)
_DOC_WRITE = errors(401, 403, 404, 409, 413, 422, 429)
_DOC_MAX_BYTES = 20 * 1024 * 1024  # 与 kb_docs.MAX_BYTES 保持一致


async def _kb_docs(request: Request) -> Any:
    """库级文档服务：与知识库服务共用一个会话，存储/向量库/审计/投递都取运行时。"""
    runtime = _runtime(request)
    async with runtime.session() as session:
        yield KnowledgeBaseDocs(
            session,
            storage=runtime.storage or get_storage(),
            store=runtime.vector,
            audit=runtime.audit,
            enqueue=runtime.enqueue,
        )


Docs = Annotated[KnowledgeBaseDocs, Depends(_kb_docs)]


def _doc_meta(request: Request) -> DocActorMeta:
    state = request.scope.get("state") or {}
    client = request.client
    return DocActorMeta(
        ip=None if client is None else client.host,
        user_agent=request.headers.get("user-agent"),
        request_id=state.get("request_id"),
    )


async def _read_body(file: UploadFile) -> bytes:
    """分块读上传内容并卡 20MB 上限（与开放接口的上传上限一致）。"""
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > _DOC_MAX_BYTES:
            raise ValidationAppError(
                message="文件超过大小上限",
                details={"errors": [{"loc": ["file"], "msg": "最大 20 MB"}]},
            )
        chunks.append(chunk)
    return b"".join(chunks)


@router.post(
    "/knowledge-bases/{kb_id}/documents",
    summary="向知识库上传文档（公共库）",
    status_code=201,
    responses=_DOC_WRITE,
)
async def upload_kb_document(
    kb_id: UUID,
    request: Request,
    user: Annotated[AuthedUser, Depends(Writer)],
    docs: Docs,
    file: Annotated[UploadFile, File()],
    title: Annotated[str | None, Form(max_length=200)] = None,
    chunk_config: Annotated[str | None, Form()] = None,
) -> JSONResponse:
    data = await _read_body(file)
    config = _parse_chunk_config(chunk_config)
    result: KbUploadResult = await docs.upload(
        kb_id,
        data,
        file.filename or "document.txt",
        title=title,
        created_by=user.id,
        is_super_admin=user.is_super_admin,
        chunk_config=config,
        meta=_doc_meta(request),
    )
    body = {
        "document": docs.doc_body(result.document),
        "duplicate": result.duplicate,
        "job_id": None if result.job is None else str(result.job.id),
    }
    return JSONResponse(jsonable_encoder(body), status_code=201)


@router.get(
    "/knowledge-bases/{kb_id}/documents/{document_id}",
    summary="知识库文档详情（公共库）",
    responses=_DOC_READ,
)
async def get_kb_document(
    kb_id: UUID,
    document_id: UUID,
    user: Annotated[AuthedUser, Depends(Reader)],
    docs: Docs,
) -> JSONResponse:
    doc = await docs.get(kb_id, document_id)
    return JSONResponse(jsonable_encoder(docs.doc_body(doc)))


class KbDocumentPatch(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    metadata: dict[str, Any] | None = None
    tags: list[str] | None = None


@router.patch(
    "/knowledge-bases/{kb_id}/documents/{document_id}",
    summary="修改知识库文档元信息（公共库）",
    responses=_DOC_WRITE,
)
async def patch_kb_document(
    kb_id: UUID,
    document_id: UUID,
    body: KbDocumentPatch,
    request: Request,
    user: Annotated[AuthedUser, Depends(Writer)],
    docs: Docs,
) -> JSONResponse:
    doc = await docs.update(
        kb_id,
        document_id,
        title=body.title,
        metadata=body.metadata,
        tags=body.tags,
        created_by=user.id,
        is_super_admin=user.is_super_admin,
        meta=_doc_meta(request),
    )
    return JSONResponse(jsonable_encoder(docs.doc_body(doc)))


@router.post(
    "/knowledge-bases/{kb_id}/documents/{document_id}/reindex",
    summary="重建知识库文档（公共库）",
    status_code=202,
    responses=_DOC_WRITE,
)
async def reindex_kb_document(
    kb_id: UUID,
    document_id: UUID,
    request: Request,
    user: Annotated[AuthedUser, Depends(Writer)],
    docs: Docs,
) -> JSONResponse:
    job = await docs.reindex(
        kb_id,
        document_id,
        created_by=user.id,
        is_super_admin=user.is_super_admin,
        meta=_doc_meta(request),
    )
    return JSONResponse({"job_id": str(job.id)}, status_code=202)


@router.delete(
    "/knowledge-bases/{kb_id}/documents/{document_id}",
    summary="删除知识库文档（公共库）",
    status_code=204,
    responses=_DOC_WRITE,
)
async def delete_kb_document(
    kb_id: UUID,
    document_id: UUID,
    request: Request,
    user: Annotated[AuthedUser, Depends(Writer)],
    docs: Docs,
) -> Response:
    await docs.delete(
        kb_id,
        document_id,
        created_by=user.id,
        is_super_admin=user.is_super_admin,
        meta=_doc_meta(request),
    )
    return Response(status_code=204)


def _parse_chunk_config(raw: str | None) -> dict[str, Any] | None:
    """表单里的 chunk_config 是 JSON 字符串；非法 JSON 直接 422 到 loc=chunk_config。"""
    if raw is None or not raw.strip():
        return None
    import json

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValidationAppError(
            message="chunk_config 不是合法 JSON",
            details={"errors": [{"loc": ["chunk_config"], "msg": str(exc)}]},
        ) from exc
    if not isinstance(parsed, dict):
        raise ValidationAppError(
            message="chunk_config 必须是对象",
            details={"errors": [{"loc": ["chunk_config"], "msg": "必须是对象"}]},
        )
    return parsed
