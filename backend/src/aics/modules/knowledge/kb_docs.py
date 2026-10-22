"""库级文档（M4.7 ⑤）：文档归属知识库，`app_id` 为空。

为什么单独一层：应用侧的知识库服务（`knowledge.service.KnowledgeService`）以 `app_id`
为作用域（`AppScopedRepository`、应用级切块配置与自定义词典），而公共知识库页上传的
文档没有应用——它属于某个库（`documents.kb_id`）。两条路径共享入库流水线
（`ingest.service` 已支持 `app_id` 为空、用平台默认切块配置构建），这里只补「建文档、
落盘、建作业、投递、列表、改元信息、重建、删除」这几步，并写审计。

归属口径（设计讨论 0012 / 迁移 0022）：
- 文档、切块、补充问题按 `kb_id` 归属；`app_id` 只记录「谁传的」，可为空；
- 去重按 `(kb_id, content_hash)`，同一份文件在一个库里只存一份；
- 检索按客服关联的库集合过滤（`rag.retrieval` 的 `kb_ids`），与上传者无关。
"""

from __future__ import annotations

import hashlib
import posixpath
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from aics.core.audit import AuditEntry, AuditSink
from aics.core.errors import DocumentNotFoundError, NotFoundError, ValidationAppError
from aics.db.models import Chunk, ChunkParent, ChunkQuestion, Document, Job, KnowledgeBase
from aics.db.models.app import App
from aics.ingest.parsers import MIME_TYPES, detect_file_type
from aics.rag.config import (
    DEFAULT_CHUNK_CONFIG,
    ChunkConfig,
    check_chunk_window,
    merge_chunk_config,
)
from aics.storage import Storage
from aics.storage.base import kb_document_key

log = structlog.get_logger(__name__)

INGEST_TASK = "aics.ingest_document"
DEFAULT_LIMIT = 20
MAX_LIMIT = 100
TITLE_MAX_CHARS = 200

AUDIT_CREATE = "kb.document.create"
AUDIT_UPDATE = "kb.document.update"
AUDIT_DELETE = "kb.document.delete"
AUDIT_REINDEX = "kb.document.reindex"


def content_hash(data: bytes) -> str:
    """与 `knowledge.service.content_hash` 同口径（入库去重用）。"""
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class ActorMeta:
    """审计里要记的请求上下文（与 apps / apikeys 的管理服务一致）。"""

    ip: str | None = None
    user_agent: str | None = None
    request_id: str | None = None


_NO_META = ActorMeta()


@dataclass(frozen=True)
class KbUploadResult:
    document: Document
    duplicate: bool = False
    job: Job | None = None


@dataclass
class KnowledgeBaseDocs:
    """知识库文档的读写入口（不含检索：检索走 `chunk_search` / `rag.retrieval`）。"""

    session: AsyncSession
    storage: Storage
    store: Any = None
    audit: AuditSink | None = None
    enqueue: Any = None

    # ------------------------------------------------------------------ 库
    async def _kb(self, kb_id: uuid.UUID) -> KnowledgeBase:
        kb = await self.session.get(KnowledgeBase, kb_id)
        if kb is None:
            raise NotFoundError(message="知识库不存在", details={"kb_id": str(kb_id)})
        return kb

    async def _app_name(self, app_id: uuid.UUID | None) -> str | None:
        if app_id is None:
            return None
        app = await self.session.get(App, app_id)
        return None if app is None else app.name

    # ------------------------------------------------------------------ 配置
    @staticmethod
    def _resolve_chunk_config(patch: dict[str, Any] | None) -> dict[str, Any] | None:
        """库级文档没有应用配置可合并：以平台默认配置为底，校验后整份落到文档上。"""
        if patch is None:
            return None
        merged = merge_chunk_config(DEFAULT_CHUNK_CONFIG, patch)
        try:
            cfg = ChunkConfig.model_validate(merged)
        except Exception as exc:  # noqa: BLE001 - pydantic 错误统一转 422
            raise ValidationAppError(
                message="切块配置不合法",
                details={"errors": [{"loc": ["chunk_config"], "msg": str(exc)}]},
            ) from exc
        check_chunk_window(cfg)
        return cfg.model_dump(mode="json")

    # ------------------------------------------------------------------ 读
    def doc_body(self, doc: Document, *, app_name: str | None = None) -> dict[str, Any]:
        return {
            "id": str(doc.id),
            "kb_id": str(doc.kb_id) if doc.kb_id else None,
            "app_id": str(doc.app_id) if doc.app_id else None,
            "app_name": app_name,
            "title": doc.title,
            "source_type": doc.source_type,
            "source_uri": doc.source_uri,
            "mime_type": doc.mime_type,
            "size_bytes": doc.size_bytes,
            "status": doc.status,
            "progress": doc.progress,
            "error": doc.error,
            "chunk_count": doc.chunk_count,
            "tags": list(doc.tags or []),
            "metadata": dict(doc.metadata_ or {}),
            "chunk_config": dict(doc.chunk_config or {}),
            "index_version": doc.index_version,
            "effective_at": doc.effective_at.isoformat() if doc.effective_at else None,
            "expires_at": doc.expires_at.isoformat() if doc.expires_at else None,
            "created_at": doc.created_at.isoformat() if doc.created_at else None,
            "updated_at": doc.updated_at.isoformat() if doc.updated_at else None,
        }

    async def list(
        self,
        kb_id: uuid.UUID,
        *,
        status: str | None = None,
        q: str | None = None,
        page: int = 1,
        page_size: int = DEFAULT_LIMIT,
    ) -> tuple[list[tuple[Document, str | None]], int]:
        await self._kb(kb_id)
        conditions = [Document.kb_id == kb_id, Document.deleted_at.is_(None)]
        if status:
            conditions.append(Document.status == status)
        if q and q.strip():
            conditions.append(Document.title.ilike(f"%{q.strip()}%"))
        total = await self.session.scalar(
            select(func.count()).select_from(Document).where(*conditions)
        )
        size = max(1, min(int(page_size), MAX_LIMIT))
        rows = (
            await self.session.execute(
                select(Document, App.name)
                .outerjoin(App, App.id == Document.app_id)
                .where(*conditions)
                .order_by(Document.created_at.desc(), Document.id.desc())
                .offset((max(1, int(page)) - 1) * size)
                .limit(size)
            )
        ).all()
        return [(row[0], row[1]) for row in rows], int(total or 0)

    async def get(self, kb_id: uuid.UUID, document_id: uuid.UUID) -> Document:
        doc = await self.session.scalar(
            select(Document).where(
                Document.id == document_id,
                Document.kb_id == kb_id,
                Document.deleted_at.is_(None),
            )
        )
        if doc is None:
            raise DocumentNotFoundError(details={"document_id": str(document_id)})
        return doc

    # ------------------------------------------------------------------ 写
    async def upload(
        self,
        kb_id: uuid.UUID,
        data: bytes,
        filename: str,
        *,
        title: str | None = None,
        created_by: uuid.UUID | None = None,
        chunk_config: dict[str, Any] | None = None,
        meta: ActorMeta = _NO_META,
    ) -> KbUploadResult:
        await self._kb(kb_id)
        ftype = detect_file_type(filename, data)
        name = posixpath.basename(filename.replace("\\", "/")) or f"document.{ftype}"
        digest = content_hash(data)
        existing = await self.session.scalar(
            select(Document).where(
                Document.kb_id == kb_id,
                Document.content_hash == digest,
                Document.deleted_at.is_(None),
            )
        )
        if existing is not None:
            return KbUploadResult(existing, duplicate=True)

        doc = Document(
            id=uuid.uuid4(),
            app_id=None,
            kb_id=kb_id,
            title=(title or name)[:TITLE_MAX_CHARS],
            source_type="file",
            source_uri=None,
            mime_type=MIME_TYPES[ftype],
            size_bytes=len(data),
            content_hash=digest,
            status="queued",
            pending_index_version=1,
            created_by=created_by,
            chunk_config=self._resolve_chunk_config(chunk_config),
        )
        key = kb_document_key(kb_id, doc.id, 1, ftype)
        doc.storage_key = key
        await self.storage.put(key, data)
        self.session.add(doc)
        job = Job(app_id=None, type="ingest", target_id=doc.id, payload={}, created_by=created_by)
        self.session.add(job)
        await self.session.flush()
        await self._audit(
            AUDIT_CREATE,
            doc,
            actor_id=created_by,
            meta=meta,
            diff={"after": {"title": doc.title, "size_bytes": doc.size_bytes}},
        )
        await self.session.commit()
        self._send(INGEST_TASK, [str(doc.id), str(job.id)])
        return KbUploadResult(doc, job=job)

    async def update(
        self,
        kb_id: uuid.UUID,
        document_id: uuid.UUID,
        *,
        title: str | None = None,
        metadata: dict[str, Any] | None = None,
        tags: Sequence[str] | None = None,
        created_by: uuid.UUID | None = None,
        meta: ActorMeta = _NO_META,
    ) -> Document:
        doc = await self.get(kb_id, document_id)
        before = {"title": doc.title, "tags": list(doc.tags or [])}
        changed = False
        if title is not None and title.strip() and title.strip() != doc.title:
            doc.title = title.strip()[:TITLE_MAX_CHARS]
            doc.indexed_title = None  # 标题变了要重建才会进向量文本
            changed = True
        if metadata is not None:
            doc.metadata_ = dict(metadata)
            changed = True
        if tags is not None:
            doc.tags = [t.strip() for t in tags if t and t.strip()]
            changed = True
        if not changed:
            return doc
        await self._audit(
            AUDIT_UPDATE,
            doc,
            actor_id=created_by,
            meta=meta,
            diff={"before": before, "after": {"title": doc.title, "tags": list(doc.tags or [])}},
        )
        await self.session.commit()
        return doc

    async def reindex(
        self,
        kb_id: uuid.UUID,
        document_id: uuid.UUID,
        *,
        created_by: uuid.UUID | None = None,
        meta: ActorMeta = _NO_META,
    ) -> Job:
        doc = await self.get(kb_id, document_id)
        if doc.status == "deleting":
            raise ValidationAppError(
                message="文档正在删除",
                details={"errors": [{"loc": ["path", "document_id"], "msg": "文档正在删除"}]},
            )
        doc.pending_index_version = (doc.index_version or 0) + 1
        doc.status, doc.progress, doc.error = "queued", 0, None
        job = Job(app_id=None, type="ingest", target_id=doc.id, payload={}, created_by=created_by)
        self.session.add(job)
        await self.session.flush()
        await self._audit(AUDIT_REINDEX, doc, actor_id=created_by, meta=meta, diff=None)
        await self.session.commit()
        self._send(INGEST_TASK, [str(doc.id), str(job.id)])
        return job

    async def delete(
        self,
        kb_id: uuid.UUID,
        document_id: uuid.UUID,
        *,
        created_by: uuid.UUID | None = None,
        meta: ActorMeta = _NO_META,
    ) -> None:
        """库级文档删除：同步清向量点、切块、补充问题、存储与行。

        库级文档没有工作流竞争（不属于某个客服、没有替换/构建冲突），直接删干净比排
        异步清理更可预期；先把状态改成 `deleting`，避免并发重建把数据写回来。
        """
        doc = await self.get(kb_id, document_id)
        doc.status = "deleting"
        await self.session.commit()

        if self.store is not None:
            await self.store.delete_document_points(doc.id)
        if doc.storage_key:
            await self.storage.delete(doc.storage_key)
        for model in (ChunkQuestion, Chunk, ChunkParent):
            await self.session.execute(delete(model).where(model.document_id == doc.id))
        await self._audit(
            AUDIT_DELETE,
            doc,
            actor_id=created_by,
            meta=meta,
            diff={"before": {"title": doc.title}},
        )
        await self.session.execute(
            update(Document)
            .where(Document.id == doc.id)
            .values(deleted_at=datetime.now(UTC), status="deleted")
        )
        await self.session.commit()

    # ------------------------------------------------------------------ 内部
    def _send(self, task: str, args: list[Any]) -> None:
        if self.enqueue is None:
            return
        try:
            self.enqueue(task, args)
        except Exception:  # noqa: BLE001 - 投递失败只记日志，作业可由扫描补
            log.warning("kb.documents.enqueue_failed", task=task)

    async def _audit(
        self,
        action: str,
        doc: Document,
        *,
        actor_id: uuid.UUID | None,
        meta: ActorMeta,
        diff: dict[str, Any] | None,
    ) -> None:
        if self.audit is None or actor_id is None:
            return
        await self.audit.write(
            self.session,
            AuditEntry(
                action=action,
                actor_id=actor_id,
                app_id=None,
                target_type="document",
                target_id=doc.id,
                diff=None if diff is None else _jsonable(diff),
                ip=meta.ip,
                user_agent=meta.user_agent,
                request_id=meta.request_id,
            ),
        )


def _jsonable(value: Any) -> Any:
    """审计 diff 里剔除 UUID / datetime 等不可 JSON 化的值（写进 jsonb 列）。"""
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    return value


__all__ = [
    "AUDIT_CREATE",
    "AUDIT_DELETE",
    "AUDIT_REINDEX",
    "AUDIT_UPDATE",
    "DEFAULT_LIMIT",
    "INGEST_TASK",
    "KbUploadResult",
    "KnowledgeBaseDocs",
    "MAX_LIMIT",
    "ActorMeta",
    "content_hash",
]
