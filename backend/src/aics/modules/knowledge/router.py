"""知识库开放接口（`/api/v1/apps/{app_id}/documents`，接口规范 §5 / 开放接口参考 §4）。

本步只做「读」：文档列表、文档详情、切块列表（写接口在 M3.2 ③）。路由只做协议层：
解析参数 → 调 `KnowledgeService` → 序列化；业务规则与错误码都在 service 里（404 跨应用、
`q` 通配符转义、只返回生效构建号的切块）。scope：`kb:read`。
"""

from __future__ import annotations

from typing import Annotated, Any, Literal
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from aics.core.security import Principal, require_scope
from aics.db.models import Chunk, Document
from aics.modules.knowledge.runtime import KnowledgeRuntime
from aics.modules.knowledge.service import PAGE_DEFAULT, PAGE_MAX

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
