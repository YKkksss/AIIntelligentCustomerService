"""管理版知识库接口（M4.3 ②）：`/api/v1/admin/apps/{app_id}/...`，JWT + 点分权限。

与开放接口（`router.py`）的区别只有三处：

- 鉴权：`require_perm("kb.read")` / `require_perm("kb.write")`，写入都带操作人（`created_by`），
  由 `KnowledgeService` 在同一条事务里写 `kb.document.*` 审计；
- 上传 / 导入 / 替换 / 重建接受 `chunk_config` 与 `reset_chunk_config`（后台改切块配置的入口）；
- 多了切块预览（`/preview`）、任务列表（`/jobs`）与命中测试（`/hit-test`），都是后台页面需要的。

开放接口的应答构造（`document_body` / `write_body` / 分页体）直接复用 `router.py` 的同一份函数，
避免两套契约各自漂移。
"""

from __future__ import annotations

import json
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, model_validator

from aics.core.config import get_settings
from aics.core.errors import ValidationAppError
from aics.core.openapi import errors
from aics.core.security import AuthedUser, require_perm
from aics.modules.knowledge.router import (
    DocumentPatch,
    Idem,
    UrlImportBody,
    _check_extension,
    _page_body,
    _parse_metadata,
    _read_upload,
    chunk_body,
    document_body,
    write_body,
)
from aics.modules.knowledge.runtime import KnowledgeRuntime
from aics.modules.knowledge.service import HIT_TEST_MAX_CHARS, TITLE_MAX_CHARS

router = APIRouter(prefix="/api/v1/admin/apps/{app_id}", tags=["admin-knowledge"])

Reader = require_perm("kb.read")
Writer = require_perm("kb.write")

_READ_ERRORS = errors(401, 403, 404, 422)
_WRITE_ERRORS = errors(401, 403, 404, 409, 413, 422, 429)


def _runtime(request: Request) -> KnowledgeRuntime:
    return request.app.state.knowledge_runtime


def _chunk_config(raw: str | None) -> dict[str, Any] | None:
    """表单里的 `chunk_config`（JSON 对象）→ dict；非法 JSON 按 422 报在 `chunk_config` 上。"""
    if raw is None or not raw.strip():
        return None
    try:
        value = json.loads(raw)
    except ValueError as e:
        raise ValidationAppError(
            message="chunk_config 不是合法 JSON",
            details={"errors": [{"loc": ["chunk_config"], "msg": str(e)}]},
        ) from e
    if not isinstance(value, dict):
        raise ValidationAppError(
            message="chunk_config 必须是 JSON 对象",
            details={"errors": [{"loc": ["chunk_config"], "msg": "必须是对象"}]},
        )
    return value


class PreviewBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chunk_config: dict[str, Any] | None = None
    reset_chunk_config: bool = False


class HitTestBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str
    overrides: dict[str, Any] | None = None
    filters: dict[str, Any] | None = None


class ChunkPatch(BaseModel):
    """切块启停 / 复核（M4.3 ③）：`review_status` 只接受 `approved`。"""

    model_config = ConfigDict(extra="forbid")

    enabled: bool | None = None
    review_status: Literal["approved"] | None = None

    @model_validator(mode="after")
    def _at_least_one(self) -> ChunkPatch:
        if self.enabled is None and self.review_status is None:
            raise ValueError("至少给出 enabled 或 review_status")
        return self


def _chunk_row(chunk: Any) -> dict[str, Any]:
    """切块应答（管理接口 §知识库）：启用态、复核态与复核时间都返回。"""
    return {
        "id": str(chunk.id),
        "document_id": str(chunk.document_id),
        "ordinal": chunk.ordinal,
        "heading_path": chunk.heading_path,
        "text": chunk.text,
        "enabled": chunk.enabled,
        "review_status": chunk.review_status,
        "flag_reason": chunk.flag_reason,
        "reviewed_at": chunk.reviewed_at,
    }


# ---------------------------------------------------------------------- 读
@router.get("/documents", summary="管理端文档列表", responses=_READ_ERRORS)
async def list_documents(
    app_id: UUID,
    request: Request,
    user: Annotated[AuthedUser, Depends(Reader)],
    status: Annotated[str | None, Query(max_length=20)] = None,
    q: Annotated[str | None, Query(max_length=100)] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 20,
) -> Any:
    async with _runtime(request).session() as session:
        result = (
            await _runtime(request)
            .service(session)
            .list_documents(app_id, status=status, q=q, page=page, page_size=page_size)
        )
    return JSONResponse(
        jsonable_encoder(_page_body(result, [document_body(d) for d in result.items]))
    )


@router.get("/documents/{document_id}", summary="管理端文档详情", responses=_READ_ERRORS)
async def get_document(
    app_id: UUID,
    document_id: UUID,
    request: Request,
    user: Annotated[AuthedUser, Depends(Reader)],
) -> Any:
    async with _runtime(request).session() as session:
        doc = await _runtime(request).service(session).get_document(app_id, document_id)
    return JSONResponse(jsonable_encoder(document_body(doc)))


@router.get("/documents/{document_id}/chunks", summary="管理端切块列表", responses=_READ_ERRORS)
async def list_chunks(
    app_id: UUID,
    document_id: UUID,
    request: Request,
    user: Annotated[AuthedUser, Depends(Reader)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 20,
) -> Any:
    async with _runtime(request).session() as session:
        page_obj = (
            await _runtime(request)
            .service(session)
            .list_chunks(app_id, document_id, page=page, page_size=page_size)
        )
    return JSONResponse(
        jsonable_encoder(_page_body(page_obj, [chunk_body(c) for c in page_obj.items]))
    )


@router.get("/chunks", summary="管理端切块查询（待复核 / 按文档）", responses=_READ_ERRORS)
async def list_app_chunks(
    app_id: UUID,
    request: Request,
    user: Annotated[AuthedUser, Depends(Reader)],
    document_id: UUID | None = None,
    review_status: Annotated[str | None, Query(pattern="^(none|flagged|approved)$")] = None,
    enabled: bool | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 20,
) -> Any:
    async with _runtime(request).session() as session:
        page_obj = (
            await _runtime(request)
            .service(session)
            .list_app_chunks(
                app_id,
                document_id=document_id,
                review_status=review_status,
                enabled=enabled,
                page=page,
                page_size=page_size,
            )
        )
    return JSONResponse(jsonable_encoder(_page_body(page_obj, page_obj.items)))


@router.get("/jobs", summary="管理端任务列表（入库 / 重建 / 清理）", responses=_READ_ERRORS)
async def list_jobs(
    app_id: UUID,
    request: Request,
    user: Annotated[AuthedUser, Depends(Reader)],
    type: Annotated[str | None, Query(max_length=40)] = None,
    status: Annotated[str | None, Query(max_length=20)] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 20,
) -> Any:
    async with _runtime(request).session() as session:
        rows, total = (
            await _runtime(request)
            .service(session)
            .list_jobs(app_id, type=type, status=status, page=page, page_size=page_size)
        )
    items = [
        {
            "id": str(j.id),
            "app_id": str(j.app_id) if j.app_id else None,
            "type": j.type,
            "status": j.status,
            "progress": j.progress,
            "target_id": str(j.target_id) if j.target_id else None,
            "error": j.error,
            "created_at": j.created_at,
            "finished_at": j.finished_at,
        }
        for j in rows
    ]
    return JSONResponse(
        jsonable_encoder({"items": items, "total": total, "page": page, "page_size": page_size})
    )


# ---------------------------------------------------------------------- 写
@router.post(
    "/documents",
    summary="管理端上传文档（multipart）",
    status_code=202,
    responses=_WRITE_ERRORS,
)
async def upload_document(
    app_id: UUID,
    request: Request,
    user: Annotated[AuthedUser, Depends(Writer)],
    idem: Idem,
    file: Annotated[UploadFile, File()],
    title: Annotated[str | None, Form(max_length=TITLE_MAX_CHARS)] = None,
    metadata: Annotated[str | None, Form()] = None,
    chunk_config: Annotated[str | None, Form()] = None,
) -> Any:
    _check_extension(file.filename)
    max_bytes = get_settings().upload_max_mb * 1024 * 1024
    data = await _read_upload(file, max_bytes)
    _parse_metadata(metadata)
    async with idem.lock(
        key_id=user.id,
        idem=request.headers.get("Idempotency-Key"),
        digest=f"{app_id}:{len(data)}:{title or ''}",
    ) as guard:
        replayed = _replayed_guard(guard)
        if replayed is not None:
            return replayed
        async with _runtime(request).session() as session:
            result = (
                await _runtime(request)
                .service(session)
                .upload_file(
                    app_id,
                    data,
                    file.filename or "",
                    title=title,
                    created_by=user.id,
                    chunk_config=_chunk_config(chunk_config),
                )
            )
    body = write_body(result.document, duplicate=result.duplicate, job=result.job)
    return JSONResponse(body, status_code=200 if result.duplicate else 202)


@router.post("/documents/url", summary="管理端 URL 导入", responses=_WRITE_ERRORS)
async def import_document_url(
    app_id: UUID,
    body: UrlImportBody,
    request: Request,
    user: Annotated[AuthedUser, Depends(Writer)],
    chunk_config: Annotated[str | None, Query()] = None,
) -> Any:
    async with _runtime(request).session() as session:
        result = (
            await _runtime(request)
            .service(session)
            .import_url(
                app_id,
                body.url,
                title=body.title,
                created_by=user.id,
                chunk_config=_chunk_config(chunk_config),
            )
        )
    return JSONResponse(
        write_body(result.document, duplicate=result.duplicate, job=result.job), status_code=202
    )


@router.put(
    "/documents/{document_id}/file",
    summary="管理端替换文档文件",
    responses=_WRITE_ERRORS,
)
async def replace_document_file(
    app_id: UUID,
    document_id: UUID,
    request: Request,
    user: Annotated[AuthedUser, Depends(Writer)],
    file: Annotated[UploadFile, File()],
    chunk_config: Annotated[str | None, Form()] = None,
    reset_chunk_config: Annotated[bool, Form()] = False,
) -> Any:
    _check_extension(file.filename)
    max_bytes = get_settings().upload_max_mb * 1024 * 1024
    data = await _read_upload(file, max_bytes)
    async with _runtime(request).session() as session:
        result = (
            await _runtime(request)
            .service(session)
            .replace_file(
                app_id,
                document_id,
                data,
                file.filename or "",
                created_by=user.id,
                chunk_config=_chunk_config(chunk_config),
                reset_chunk_config=reset_chunk_config,
            )
        )
    return JSONResponse(write_body(result.document, unchanged=result.unchanged, job=result.job))


@router.post(
    "/documents/{document_id}/reindex",
    summary="管理端重建文档（可改切块配置）",
    responses=_WRITE_ERRORS,
)
async def reindex_document(
    app_id: UUID,
    document_id: UUID,
    request: Request,
    user: Annotated[AuthedUser, Depends(Writer)],
    body: PreviewBody | None = None,
) -> Any:
    body = body or PreviewBody()
    async with _runtime(request).session() as session:
        job = (
            await _runtime(request)
            .service(session)
            .rebuild_document(
                app_id,
                document_id,
                created_by=user.id,
                chunk_config=body.chunk_config,
                reset_chunk_config=body.reset_chunk_config,
            )
        )
    return JSONResponse({"job_id": str(job.id), "status": job.status}, status_code=202)


@router.patch(
    "/documents/{document_id}", summary="管理端修改文档标题与元数据", responses=_WRITE_ERRORS
)
async def patch_document(
    app_id: UUID,
    document_id: UUID,
    body: DocumentPatch,
    request: Request,
    user: Annotated[AuthedUser, Depends(Writer)],
) -> Any:
    async with _runtime(request).session() as session:
        doc = (
            await _runtime(request)
            .service(session)
            .update_document(
                app_id, document_id, title=body.title, metadata=body.metadata, created_by=user.id
            )
        )
    return JSONResponse(jsonable_encoder(document_body(doc)))


@router.delete(
    "/documents/{document_id}",
    summary="管理端删除文档（异步清理）",
    status_code=202,
    responses=_WRITE_ERRORS,
)
async def delete_document(
    app_id: UUID,
    document_id: UUID,
    request: Request,
    user: Annotated[AuthedUser, Depends(Writer)],
) -> Any:
    async with _runtime(request).session() as session:
        job = (
            await _runtime(request)
            .service(session)
            .delete_document(app_id, document_id, created_by=user.id)
        )
    return JSONResponse({"job_id": str(job.id), "status": job.status}, status_code=202)


# ---------------------------------------------------------------------- 切块复核（M4.3 ③）
@router.patch("/chunks/{chunk_id}", summary="管理端切块启停与复核", responses=_WRITE_ERRORS)
async def patch_chunk(
    app_id: UUID,
    chunk_id: UUID,
    body: ChunkPatch,
    request: Request,
    user: Annotated[AuthedUser, Depends(Writer)],
) -> Any:
    async with _runtime(request).session() as session:
        chunk = (
            await _runtime(request)
            .service(session)
            .update_chunk(
                app_id,
                chunk_id,
                enabled=body.enabled,
                review_status=body.review_status,
                created_by=user.id,
            )
        )
    return JSONResponse(jsonable_encoder(_chunk_row(chunk)))


# ---------------------------------------------------------------------- 预览与命中测试
@router.post("/documents/preview", summary="上传前预览切块", responses=_WRITE_ERRORS)
async def preview_upload(
    app_id: UUID,
    request: Request,
    user: Annotated[AuthedUser, Depends(Writer)],
    file: Annotated[UploadFile, File()],
    chunk_config: Annotated[str | None, Form()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> Any:
    _check_extension(file.filename)
    data = await _read_upload(file, get_settings().upload_max_mb * 1024 * 1024)
    async with _runtime(request).session() as session:
        result = (
            await _runtime(request)
            .service(session)
            .preview_upload(
                app_id,
                data,
                file.filename or "",
                chunk_config=_chunk_config(chunk_config),
                limit=limit,
            )
        )
    return JSONResponse(jsonable_encoder(result))


@router.post(
    "/documents/{document_id}/preview", summary="按新配置预览已有文档切块", responses=_READ_ERRORS
)
async def preview_document(
    app_id: UUID,
    document_id: UUID,
    request: Request,
    user: Annotated[AuthedUser, Depends(Reader)],
    body: PreviewBody | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> Any:
    body = body or PreviewBody()
    async with _runtime(request).session() as session:
        result = (
            await _runtime(request)
            .service(session)
            .preview_document(app_id, document_id, chunk_config=body.chunk_config, limit=limit)
        )
    return JSONResponse(jsonable_encoder(result))


@router.post("/hit-test", summary="检索命中测试（不写会话与统计）", responses=_READ_ERRORS)
async def hit_test(
    app_id: UUID,
    body: HitTestBody,
    request: Request,
    user: Annotated[AuthedUser, Depends(Reader)],
) -> Any:
    query = body.query.strip()
    if not query or len(query) > HIT_TEST_MAX_CHARS:
        from aics.core.errors import ValidationAppError

        raise ValidationAppError(
            message=f"query 长度需在 1..{HIT_TEST_MAX_CHARS} 之间",
            details={"errors": [{"loc": ["query"], "msg": "长度不合法"}]},
        )
    runtime = _runtime(request)
    reranker = runtime.reranker
    if reranker is None:
        from aics.providers import get_reranker

        reranker = get_reranker()
    async with runtime.session() as session:
        result = await runtime.service(session).hit_test(
            app_id, query, reranker=reranker, overrides=body.overrides, filters=body.filters
        )
    return JSONResponse(jsonable_encoder(result))


def _replayed_guard(guard: Any) -> JSONResponse | None:
    """幂等回放：管理接口没有 `Principal`，头里只用 `X-RateLimit-*` 之外的统一头。"""
    if guard.replay is None:
        return None
    return JSONResponse(guard.replay.body, status_code=guard.replay.status)
