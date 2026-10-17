"""知识库开放接口（`/api/v1/apps/{app_id}/documents`，接口规范 §5 / 开放接口参考 §4）。

读接口（`kb:read`）：文档列表、文档详情、切块列表。写接口（`kb:write`）：上传、URL 导入、
替换、重建、改标题/元数据、删除。路由只做协议层：解析参数 → 调 `KnowledgeService` → 序列化；
业务规则与错误码都在 service 里（404 跨应用、`q` 通配符转义、只返回生效构建号的切块、
`409 DOCUMENT_BUSY` / `409 DUPLICATE_DOCUMENT`）。上传按 1 MB 分块读取，超过
`AICS_UPLOAD_MAX_MB` 立即 `413 FILE_TOO_LARGE`（不整体读入内存）；写接口支持
`Idempotency-Key`（`core.idempotency`）。
"""

from __future__ import annotations

import json
from typing import Annotated, Any, Literal
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from aics.core.config import get_settings
from aics.core.errors import FileTooLargeError, ValidationAppError
from aics.core.idempotency import HEADER as IDEMPOTENCY_HEADER
from aics.core.idempotency import Idempotency, IdempotencyGuard, digest_json, digest_upload
from aics.core.security import Principal, require_scope
from aics.db.models import Chunk, Document
from aics.ingest.parsers import EXTENSIONS, UnsupportedFileTypeError
from aics.modules.knowledge.runtime import KnowledgeRuntime
from aics.modules.knowledge.service import PAGE_DEFAULT, PAGE_MAX, TITLE_MAX_CHARS

log = structlog.get_logger()

router = APIRouter(prefix="/api/v1/apps/{app_id}", tags=["knowledge"])

#: 与 `db.models.knowledge.DOCUMENT_STATUSES` 必须一致（单元测试逐一比对）
DocumentStatus = Literal["queued", "parsing", "indexing", "done", "failed", "deleting"]

QUERY_MAX_CHARS = 100


def document_body(doc: Document) -> dict[str, Any]:
    return {
        "id": str(doc.id),
        "title": doc.title,
        "source_type": doc.source_type,
        "source_uri": doc.source_uri,
        "mime_type": doc.mime_type,
        "size_bytes": doc.size_bytes,
        "status": doc.status,
        "progress": doc.progress,
        "error": doc.error,
        "chunk_count": doc.chunk_count,
        "index_version": doc.index_version,
        "tags": list(doc.tags or []),
        "created_at": doc.created_at,
        "updated_at": doc.updated_at,
    }


def chunk_body(chunk: Chunk) -> dict[str, Any]:
    return {
        "id": str(chunk.id),
        "ordinal": chunk.ordinal,
        "heading_path": chunk.heading_path,
        "text": chunk.text,
        "tokens": chunk.tokens,
        "page_from": chunk.page_from,
        "page_to": chunk.page_to,
        "enabled": chunk.enabled,
        "review_status": chunk.review_status,
        "flag_reason": chunk.flag_reason,
    }


def _page_body(page: Any, items: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "items": items,
        "total": page.total,
        "page": page.page,
        "page_size": page.page_size,
    }


@router.get("/documents")
async def list_documents(
    app_id: UUID,
    request: Request,
    principal: Annotated[Principal, Depends(require_scope("kb:read"))],
    status: Annotated[DocumentStatus | None, Query()] = None,
    q: Annotated[str | None, Query(max_length=QUERY_MAX_CHARS)] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=PAGE_MAX)] = PAGE_DEFAULT,
) -> Any:
    runtime: KnowledgeRuntime = request.app.state.knowledge_runtime
    async with runtime.session() as session:
        result = await runtime.service(session).list_documents(
            app_id, status=status, q=q, page=page, page_size=page_size
        )
    return JSONResponse(
        jsonable_encoder(_page_body(result, [document_body(d) for d in result.items])),
        headers=principal.rate_headers,
    )


@router.get("/documents/{document_id}")
async def get_document(
    app_id: UUID,
    document_id: UUID,
    request: Request,
    principal: Annotated[Principal, Depends(require_scope("kb:read"))],
) -> Any:
    runtime: KnowledgeRuntime = request.app.state.knowledge_runtime
    async with runtime.session() as session:
        doc = await runtime.service(session).get_document(app_id, document_id)
    return JSONResponse(jsonable_encoder(document_body(doc)), headers=principal.rate_headers)


@router.get("/documents/{document_id}/chunks")
async def list_chunks(
    app_id: UUID,
    document_id: UUID,
    request: Request,
    principal: Annotated[Principal, Depends(require_scope("kb:read"))],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=PAGE_MAX)] = PAGE_DEFAULT,
) -> Any:
    runtime: KnowledgeRuntime = request.app.state.knowledge_runtime
    async with runtime.session() as session:
        result = await runtime.service(session).list_chunks(
            app_id, document_id, page=page, page_size=page_size
        )
    return JSONResponse(
        jsonable_encoder(_page_body(result, [chunk_body(c) for c in result.items])),
        headers=principal.rate_headers,
    )


# ---------------------------------------------------------------------- 写
UPLOAD_CHUNK_BYTES = 1024 * 1024
MAX_URL_CHARS = 2048


def idempotency_of(request: Request) -> Idempotency:
    """幂等键实例：生命周期装到 `app.state.idempotency`；缺失（单测不带 lifespan）时退化为不启用。"""
    found = getattr(request.app.state, "idempotency", None)
    return found if found is not None else Idempotency(None, prefix=get_settings().redis_prefix)


Idem = Annotated[Idempotency, Depends(idempotency_of)]


class UrlImportBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str = Field(min_length=1, max_length=MAX_URL_CHARS)
    title: str | None = Field(default=None, max_length=TITLE_MAX_CHARS)


class DocumentPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, max_length=TITLE_MAX_CHARS)
    metadata: dict[str, Any] | None = None


def write_body(
    doc: Document, *, duplicate: bool = False, unchanged: bool = False, job: Any = None
) -> dict[str, Any]:
    """写接口应答（开放接口 §4 上传示例的超集）。"""
    return {
        "id": str(doc.id),
        "title": doc.title,
        "status": doc.status,
        "progress": doc.progress,
        "duplicate": duplicate,
        "unchanged": unchanged,
        "job_id": str(job.id) if job is not None else None,
    }


def _check_extension(filename: str | None) -> None:
    """扩展名快速拒绝（魔数仍由 `ingest.parsers.detect_file_type` 判定）。"""
    name = filename or ""
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext not in EXTENSIONS:
        raise UnsupportedFileTypeError(message=f"不支持的文件类型：{ext or '(无扩展名)'}")


async def _read_upload(file: UploadFile, max_bytes: int) -> bytes:
    """按 1 MB 分块读取，超过上限立即 413（不整体读入内存）。"""
    pieces: list[bytes] = []
    total = 0
    while True:
        piece = await file.read(UPLOAD_CHUNK_BYTES)
        if not piece:
            break
        total += len(piece)
        if total > max_bytes:
            raise FileTooLargeError(details={"max_bytes": max_bytes})
        pieces.append(piece)
    if not pieces:
        raise ValidationAppError(
            message="文件为空", details={"errors": [{"loc": ["file"], "msg": "文件为空"}]}
        )
    return b"".join(pieces)


def _parse_metadata(raw: str | None) -> dict[str, Any] | None:
    if raw is None or not raw.strip():
        return None
    try:
        value = json.loads(raw)
    except ValueError as e:
        raise ValidationAppError(
            message="metadata 不是合法 JSON",
            details={"errors": [{"loc": ["metadata"], "msg": str(e)}]},
        ) from e
    if not isinstance(value, dict):
        raise ValidationAppError(
            message="metadata 必须是 JSON 对象",
            details={"errors": [{"loc": ["metadata"], "msg": "必须是对象"}]},
        )
    return value


def _replayed(guard: IdempotencyGuard, principal: Principal) -> JSONResponse | None:
    if guard.replay is None:
        return None
    log.info("knowledge.idempotent_replay")
    return JSONResponse(
        jsonable_encoder(guard.replay.body),
        status_code=guard.replay.status,
        headers=principal.rate_headers,
    )


@router.post("/documents")
async def upload_document(
    app_id: UUID,
    request: Request,
    principal: Annotated[Principal, Depends(require_scope("kb:write"))],
    idem: Idem,
    file: Annotated[UploadFile, File()],
    title: Annotated[str | None, Form(max_length=TITLE_MAX_CHARS)] = None,
    metadata: Annotated[str | None, Form()] = None,
) -> Any:
    """上传文档（FR-KB-01）：落盘 + 建 job + 投递入队；同应用内容重复返回 200 + `duplicate`。"""
    _check_extension(file.filename)
    settings = get_settings()
    data = await _read_upload(file, settings.upload_max_mb * 1024 * 1024)
    meta = _parse_metadata(metadata)
    digest = digest_upload(data, {"title": title, "metadata": meta})
    runtime: KnowledgeRuntime = request.app.state.knowledge_runtime
    async with idem.lock(
        key_id=principal.key_id, idem=request.headers.get(IDEMPOTENCY_HEADER), digest=digest
    ) as guard:
        replayed = _replayed(guard, principal)
        if replayed is not None:
            return replayed
        async with runtime.session() as session:
            result = await runtime.service(session).upload_file(
                app_id, data, file.filename or "", title=title
            )
        status = 200 if result.duplicate else 202
        body = write_body(result.document, duplicate=result.duplicate, job=result.job)
        await guard.record(status, body)
    return JSONResponse(jsonable_encoder(body), status_code=status, headers=principal.rate_headers)


@router.post("/documents/url")
async def import_document_url(
    app_id: UUID,
    body: UrlImportBody,
    request: Request,
    principal: Annotated[Principal, Depends(require_scope("kb:write"))],
    idem: Idem,
) -> Any:
    """URL 导入（FR-KB-02）：抓取在 service 里做（`core.netguard` 校验 + 15 s 超时）。"""
    digest = digest_json({"url": body.url, "title": body.title})
    runtime: KnowledgeRuntime = request.app.state.knowledge_runtime
    async with idem.lock(
        key_id=principal.key_id, idem=request.headers.get(IDEMPOTENCY_HEADER), digest=digest
    ) as guard:
        replayed = _replayed(guard, principal)
        if replayed is not None:
            return replayed
        async with runtime.session() as session:
            result = await runtime.service(session).import_url(app_id, body.url, title=body.title)
        status = 200 if result.duplicate else 202
        response_body = write_body(result.document, duplicate=result.duplicate, job=result.job)
        await guard.record(status, response_body)
    return JSONResponse(
        jsonable_encoder(response_body), status_code=status, headers=principal.rate_headers
    )


@router.put("/documents/{document_id}/file")
async def replace_document_file(
    app_id: UUID,
    document_id: UUID,
    request: Request,
    principal: Annotated[Principal, Depends(require_scope("kb:write"))],
    idem: Idem,
    file: Annotated[UploadFile, File()],
) -> Any:
    """替换文档（FR-KB-08）：文档 ID 不变；内容相同 → 200 `unchanged`；与别的文档重复 → 409。"""
    _check_extension(file.filename)
    settings = get_settings()
    data = await _read_upload(file, settings.upload_max_mb * 1024 * 1024)
    digest = digest_upload(data, {"document_id": str(document_id)})
    runtime: KnowledgeRuntime = request.app.state.knowledge_runtime
    async with idem.lock(
        key_id=principal.key_id, idem=request.headers.get(IDEMPOTENCY_HEADER), digest=digest
    ) as guard:
        replayed = _replayed(guard, principal)
        if replayed is not None:
            return replayed
        async with runtime.session() as session:
            result = await runtime.service(session).replace_file(
                app_id, document_id, data, file.filename or ""
            )
        status = 200 if result.unchanged else 202
        body = write_body(result.document, unchanged=result.unchanged, job=result.job)
        await guard.record(status, body)
    return JSONResponse(jsonable_encoder(body), status_code=status, headers=principal.rate_headers)


@router.post("/documents/{document_id}/reindex")
async def reindex_document(
    app_id: UUID,
    document_id: UUID,
    request: Request,
    principal: Annotated[Principal, Depends(require_scope("kb:write"))],
) -> Any:
    """重建文档（FR-KB-06）：202 + job；构建期间旧版本继续可检索。"""
    runtime: KnowledgeRuntime = request.app.state.knowledge_runtime
    async with runtime.session() as session:
        job = await runtime.service(session).rebuild_document(app_id, document_id)
    body = {"id": str(document_id), "job_id": str(job.id)}
    return JSONResponse(jsonable_encoder(body), status_code=202, headers=principal.rate_headers)


@router.patch("/documents/{document_id}")
async def patch_document(
    app_id: UUID,
    document_id: UUID,
    body: DocumentPatch,
    request: Request,
    principal: Annotated[Principal, Depends(require_scope("kb:write"))],
) -> Any:
    """改标题 / 元数据（FR-KB-01）：立即生效，无需重建。"""
    if body.title is None and body.metadata is None:
        raise ValidationAppError(
            message="至少要提供 title 或 metadata",
            details={"errors": [{"loc": ["body"], "msg": "title 与 metadata 不能同时为空"}]},
        )
    runtime: KnowledgeRuntime = request.app.state.knowledge_runtime
    async with runtime.session() as session:
        doc = await runtime.service(session).update_document(
            app_id, document_id, title=body.title, metadata=body.metadata
        )
    return JSONResponse(jsonable_encoder(document_body(doc)), headers=principal.rate_headers)


@router.delete("/documents/{document_id}", status_code=204, response_class=Response)
async def delete_document(
    app_id: UUID,
    document_id: UUID,
    request: Request,
    principal: Annotated[Principal, Depends(require_scope("kb:write"))],
) -> Response:
    """删除文档（FR-KB-09）：204；立即不可检索，异步清理（`aics.cleanup_document`）。"""
    runtime: KnowledgeRuntime = request.app.state.knowledge_runtime
    async with runtime.session() as session:
        await runtime.service(session).delete_document(app_id, document_id)
    return Response(status_code=204, headers=principal.rate_headers)
