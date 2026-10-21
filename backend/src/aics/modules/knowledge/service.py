"""knowledge 服务：上传（去重）、URL 导入、替换、删除、重建、重新分词（文档处理流水线 §3.3–§6）。

M1 阶段只有 service 与 `aics-cli`，HTTP 路由随 M3（开放接口 `/api/v1/apps/{app_id}/documents`）
与 M4（后台）接入；路由层把这里的异常映射为接口规范中的错误码。
任务按任务名投递（`enqueue(task_name, args)`），本模块不 import `aics.tasks`。
"""

from __future__ import annotations

import hashlib
import json
import posixpath
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import structlog
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aics.core.audit import AuditEntry, AuditSink
from aics.core.config import get_settings
from aics.core.errors import (
    AppError,
    DocumentNotFoundError,
    FileTooLargeError,
    NotFoundError,
    ValidationAppError,
)
from aics.core.simhash import hamming, near_duplicate, to_unsigned
from aics.core.tokens import count_tokens
from aics.db.models import App, Chunk, ChunkQuestion, Document, Job
from aics.db.models.knowledge import MAX_TAGS, QUESTION_MAX_CHARS, QUESTION_SOURCES, TAG_MAX_CHARS
from aics.ingest.chunker import ChunkOptions, split_document
from aics.ingest.indexer import EmbedItem, build_embed_text, embed_and_upsert
from aics.ingest.parsers import MIME_TYPES, detect_file_type, parse_bytes
from aics.ingest.parsers.base import ParsedDoc
from aics.ingest.parsers.url import FetchedUrl, fetch_url
from aics.ingest.questions import QuestionTarget, text_hash, upsert_question_points
from aics.ingest.service import (
    OVERRIDE_KEY,
    PREVIEW_DEFAULT_LIMIT,
    preview_result,
    search_text_for,
)
from aics.modules.knowledge.chunk_search import PgChunkSearch, retrievable
from aics.modules.knowledge.repository import (
    ChunkQuestionRepository,
    ChunkRepository,
    DocumentRepository,
    JobRepository,
)
from aics.providers.base import EmbeddingProvider, RerankProvider
from aics.rag.config import (
    AppRagConfig,
    ChunkConfig,
    ConfigValidationError,
    check_chunk_window,
    merge_chunk_config,
)
from aics.rag.retrieval import Candidate, EmbeddingVectorSearch, run_retrieval
from aics.storage import Storage
from aics.storage.base import document_key
from aics.vectorstore import QdrantVectorStore

log = structlog.get_logger()

INGEST_TASK = "aics.ingest_document"
CLEANUP_TASK = "aics.cleanup_document"
RETOKENIZE_TASK = "aics.retokenize_app"
RECONCILE_TASK = "aics.reconcile"
TITLE_MAX_CHARS = 200
#: `PATCH /documents/{id}` 的 metadata 上限（序列化后字节数，防滥用）
METADATA_MAX_BYTES = 8192
BUSY_STATUSES = frozenset({"queued", "parsing", "indexing", "deleting"})
#: 任务名 → 队列（与 `aics.tasks.celery_app.task_routes` 一致；`aics-cli` 复用同一份表）
TASK_QUEUES = {
    INGEST_TASK: "ingest",
    RETOKENIZE_TASK: "ingest",
    CLEANUP_TASK: "maintenance",
    RECONCILE_TASK: "maintenance",
}

Enqueue = Callable[[str, Sequence[Any]], None]
Fetcher = Callable[[str], Awaitable[FetchedUrl]]


class DocumentBusyError(AppError):
    code, status, message = "DOCUMENT_BUSY", 409, "文档正在构建或删除中，请稍后再试"


class DuplicateDocumentError(AppError):
    code, status, message = "DUPLICATE_DOCUMENT", 409, "新文件与本应用另一文档内容相同"


class ChunkNotFoundError(NotFoundError):
    code, message = "CHUNK_NOT_FOUND", "切块不存在或不属于当前生效版本"


class QuestionNotFoundError(NotFoundError):
    code, message = "QUESTION_NOT_FOUND", "补充问题不存在"


MAX_QUESTIONS_PER_CHUNK = 20
HIT_TEST_MAX_CHARS = 500
HIT_TEST_PREVIEW_CHARS = 300
REVIEW_APPROVED = "approved"
CHUNK_TEXT_MAX_CHARS = 4000
HEADING_MAX_CHARS = 200
CHUNK_MANUAL = "manual"
CHUNK_EDITED = "edited"
KEEP: Any = object()  # PATCH 里区分「没传」与「传了 null」


def normalize_questions(questions: Sequence[str]) -> list[str]:
    """Strip, collapse inner whitespace, drop empties, dedup; each 1..QUESTION_MAX_CHARS."""
    if isinstance(questions, str):
        raise ValidationAppError(message="questions 必须是字符串数组")
    out = list(
        dict.fromkeys(" ".join(q.split()) for q in questions if isinstance(q, str) and q.strip())
    )
    if not out:
        raise ValidationAppError(message="至少需要一个非空问题")
    if any(len(q) > QUESTION_MAX_CHARS for q in out):
        raise ValidationAppError(message=f"单个问题最多 {QUESTION_MAX_CHARS} 字")
    return out


@dataclass(frozen=True)
class UploadResult:
    document: Document
    duplicate: bool  # true：同应用已有相同内容（SHA-256），返回已有文档，不建任务
    job: Job | None


@dataclass(frozen=True)
class ReplaceResult:
    document: Document
    unchanged: bool  # true：新文件与本文档当前内容相同（200 {"unchanged": true}），不构建
    job: Job | None


@dataclass
class RebuildAppResult:
    job: Job  # reindex_app 汇总任务
    jobs: list[Job] = field(default_factory=list)  # 每个文档的 reindex_document
    skipped: list[UUID] = field(default_factory=list)  # 构建中 / 无来源的文档
    # 有文档级 chunk_config 覆盖、不受应用 chunk_config 影响而未重建的文档（include_overridden=False 时）
    skipped_override: list[UUID] = field(default_factory=list)


def content_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


PAGE_DEFAULT = 20
PAGE_MAX = 100


@dataclass(frozen=True)
class DocumentPage:
    items: list[Document]
    total: int
    page: int
    page_size: int


@dataclass(frozen=True)
class ChunkPage:
    items: list[Chunk]
    total: int
    page: int
    page_size: int


@dataclass(frozen=True)
class AppChunkPage:
    """管理版跨文档切块分页（M4.3 ③）：条目已带文档标题，直接可序列化。"""

    items: list[dict[str, Any]]
    total: int
    page: int
    page_size: int


def page_args(page: int, page_size: int) -> tuple[int, int, int]:
    """页码分页归一化（接口规范 §5）：page>=1、1<=page_size<=100，返回 (page, page_size, offset)。"""
    p = max(int(page), 1)
    size = min(max(int(page_size), 1), PAGE_MAX)
    return p, size, (p - 1) * size


def _now() -> datetime:
    return datetime.now(UTC)


def normalize_tags(tags: Sequence[str]) -> list[str]:
    """Strip, drop empties, dedup (order kept); ≤ MAX_TAGS tags of ≤ TAG_MAX_CHARS chars."""
    if isinstance(tags, str):
        raise ValidationAppError(message="tags 必须是字符串数组")
    out = list(dict.fromkeys(t.strip() for t in tags if isinstance(t, str) and t.strip()))
    if len(out) > MAX_TAGS:
        raise ValidationAppError(message=f"标签最多 {MAX_TAGS} 个")
    if any(len(t) > TAG_MAX_CHARS for t in out):
        raise ValidationAppError(message=f"单个标签最多 {TAG_MAX_CHARS} 字")
    return out


def _iso_or_none(value: datetime | None) -> str | None:
    """审计 diff 里的时间一律用 ISO 字符串（jsonb 不能直接存 datetime）。"""
    return value.isoformat() if value is not None else None


def _clean_title(title: str | None, fallback: str) -> str:
    t = (title or "").strip()[:TITLE_MAX_CHARS]
    return t or fallback[:TITLE_MAX_CHARS]


class KnowledgeService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        storage: Storage,
        store: QdrantVectorStore | None = None,
        enqueue: Enqueue | None = None,
        fetcher: Fetcher | None = None,
        embedder: EmbeddingProvider | None = None,
        audit: AuditSink | None = None,
    ) -> None:
        self.session = session
        self.embedder = embedder  # needed only for supplementary questions (M2.2b)
        self.storage = storage
        self.store = store
        self.enqueue = enqueue
        self.fetcher = fetcher or self._fetch
        self.audit = audit
        self.docs = DocumentRepository(session)
        self.chunks = ChunkRepository(session)
        self.jobs = JobRepository(session)
        self.questions = ChunkQuestionRepository(session)

    # ------------------------------------------------------------------ 公共
    @staticmethod
    async def _fetch(url: str) -> FetchedUrl:
        return await fetch_url(url, allowlist=get_settings().url_fetch_allowlist)

    async def _app(self, app_id: UUID) -> App:
        app = await self.session.get(App, app_id)
        if app is None or app.status == "deleting":
            raise NotFoundError(code="APP_NOT_FOUND", message="应用不存在")
        return app

    async def _doc(self, app_id: UUID, document_id: UUID, *, lock: bool = False) -> Document:
        doc = await (
            self.docs.get_for_update(app_id, document_id)
            if lock
            else self.docs.get(app_id, document_id)
        )
        if doc is None:  # deleting 中的文档仍返回，由调用方报 409 DOCUMENT_BUSY
            raise DocumentNotFoundError(details={"document_id": str(document_id)})
        return doc

    def _send(self, task: str, args: Sequence[Any]) -> None:
        if self.enqueue is not None:
            self.enqueue(task, list(args))

    async def _audit(
        self,
        action: str,
        app_id: UUID,
        *,
        actor_id: UUID | None,
        target_id: UUID | None = None,
        diff: dict[str, Any] | None = None,
        target_type: str = "document",
    ) -> None:
        """写一条审计（M4.3 ②）：与业务写在同一事务里，提交点在调用方。

        只有管理版接口会传 `actor_id`；开放接口的调用方不传，因而不产生审计行。
        """
        if self.audit is None or actor_id is None:
            return
        await self.audit.write(
            self.session,
            AuditEntry(
                action=action,
                actor_id=actor_id,
                app_id=app_id,
                target_type=target_type,
                target_id=target_id,
                diff=diff,
            ),
        )

    async def preview_upload(
        self,
        app_id: UUID,
        data: bytes,
        filename: str,
        *,
        chunk_config: Mapping[str, Any] | None = None,
        limit: int = PREVIEW_DEFAULT_LIMIT,
    ) -> dict[str, Any]:
        """上传前预览切块（M4.3 ②）：只解析 + 切块，不落盘、不建任务。"""
        app = await self._app(app_id)
        cfg = self._resolve_chunk_config(app, chunk_config or {})
        self._check_size(data)
        ftype = detect_file_type(filename, data)
        name = posixpath.basename(filename.replace("\\", "/")) or f"document.{ftype}"
        parsed = parse_bytes(data, ftype)
        result = split_document(parsed, ChunkOptions.from_mapping(cfg))
        return preview_result(result, title=_clean_title(None, name), chunk_config=cfg, limit=limit)

    async def preview_document(
        self,
        app_id: UUID,
        document_id: UUID,
        *,
        chunk_config: Mapping[str, Any] | None = None,
        limit: int = PREVIEW_DEFAULT_LIMIT,
    ) -> dict[str, Any]:
        """已有文档按新配置预览（M4.3 ②）：缺省用文档生效配置，否则用应用配置。"""
        app = await self._app(app_id)
        doc = await self._doc(app_id, document_id)
        base = doc.chunk_config if doc.chunk_config is not None else dict(app.chunk_config)
        cfg = (
            self._resolve_chunk_config(app, chunk_config, base)
            if chunk_config is not None
            else dict(base)
        )
        parsed = await self._load_document(doc)
        result = split_document(parsed, ChunkOptions.from_mapping(cfg))
        return preview_result(result, title=doc.title, chunk_config=cfg, limit=limit)

    async def _load_document(self, doc: Document) -> ParsedDoc:
        """按文档来源取原文（文件快照优先，URL 导入的文档回落抓取）。"""
        if doc.storage_key:
            data = await self.storage.get(doc.storage_key)
            name = posixpath.basename(doc.storage_key) or "document"
            return parse_bytes(data, detect_file_type(name, data))
        if doc.source_uri:
            fetched = await self.fetcher(doc.source_uri)
            return parse_bytes(fetched.content, fetched.file_type)
        raise ValidationAppError(message="文档没有可读取的来源")

    async def list_jobs(
        self,
        app_id: UUID,
        *,
        type: str | None = None,
        status: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[Job], int]:
        """应用的任务列表（M4.3 ②，新→旧）：前端用来轮询入库 / 清理进度。"""
        where = [Job.app_id == app_id]
        if type:
            where.append(Job.type == type)
        if status:
            where.append(Job.status == status)
        total = await self.session.scalar(select(func.count()).select_from(Job).where(*where))
        rows = await self.session.scalars(
            select(Job)
            .where(*where)
            .order_by(Job.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        return list(rows), int(total or 0)

    async def _existing(self, app_id: UUID, digest: str) -> UploadResult | None:
        doc = await self.docs.get_by_hash(app_id, digest)
        if doc is None:
            return None
        if doc.status == "deleting":
            raise DocumentBusyError(message="相同内容的文档正在删除，请稍后再上传")
        return UploadResult(doc, True, None)

    @staticmethod
    def _check_size(data: bytes) -> None:
        max_bytes = get_settings().upload_max_mb * 1024 * 1024
        if not data:
            raise ValidationAppError(message="文件为空")
        if len(data) > max_bytes:
            # 与 HTTP 入口同一码值（ISSUE-32）：服务层也不再用 422。
            raise FileTooLargeError(details={"max_bytes": max_bytes})

    @staticmethod
    def _resolve_chunk_config(
        app: App, patch: Mapping[str, Any], base: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        """文档级 chunk_config：patch 合并到 base（缺省为应用配置；带 preset 时以预设为底），
        经 `ChunkConfig` 校验与上下文窗口校验后返回完整配置（保存的是完整快照）。"""
        merged = merge_chunk_config(base if base is not None else app.chunk_config, patch)
        try:
            cfg = ChunkConfig.model_validate(merged)
        except ValidationError as e:
            errors = [
                {"loc": ["chunk_config", *map(str, err["loc"])], "msg": err["msg"]}
                for err in e.errors()
            ]
            raise ValidationAppError(message="切块配置校验失败", details={"errors": errors}) from e
        settings = get_settings()
        try:
            check_chunk_window(
                rag=AppRagConfig.model_validate(app.rag_config),
                chunk=cfg,
                models=settings.models,
                default_llm_model=settings.llm_model,
            )
        except ConfigValidationError as e:
            raise ValidationAppError(
                message="切块配置校验失败",
                details={"errors": [{"loc": list(i.loc), "msg": i.msg} for i in e.issues]},
            ) from e
        return cfg.model_dump(mode="json")

    def _override_payload(
        self,
        app: App,
        doc: Document | None,
        chunk_config: Mapping[str, Any] | None,
        reset_chunk_config: bool,
    ) -> dict[str, Any]:
        """replace / rebuild 携带的覆盖变更（构建成功切换时写入文档）；无变更返回空 dict。"""
        if reset_chunk_config and chunk_config is not None:
            raise ValidationAppError(message="chunk_config 与 reset_chunk_config 不能同时指定")
        if reset_chunk_config:
            return {OVERRIDE_KEY: None}
        if chunk_config is not None:
            base = doc.chunk_config if doc is not None else None
            return {OVERRIDE_KEY: self._resolve_chunk_config(app, chunk_config, base)}
        return {}

    @staticmethod
    def _next_build(doc: Document) -> int:
        return max(doc.index_version, doc.pending_index_version or 0) + 1

    # ------------------------------------------------------------------ 新建
    async def _create(
        self,
        app_id: UUID,
        data: bytes,
        ftype: str,
        *,
        title: str,
        source_type: str,
        source_uri: str | None,
        created_by: UUID | None,
        chunk_config: dict[str, Any] | None = None,
    ) -> UploadResult:
        digest = content_hash(data)
        found = await self._existing(app_id, digest)
        if found is not None:
            return found
        doc = Document(
            app_id=app_id,
            title=title,
            source_type=source_type,
            source_uri=source_uri,
            mime_type=MIME_TYPES[ftype],
            size_bytes=len(data),
            content_hash=digest,
            status="queued",
            pending_index_version=1,
            created_by=created_by,
            chunk_config=chunk_config,
        )
        key: str | None = None
        try:
            await self.docs.add(app_id, doc)
            key = document_key(app_id, doc.id, 1, ftype)
            doc.storage_key = key
            await self.storage.put(key, data)
            job = Job(
                app_id=app_id, type="ingest", target_id=doc.id, payload={}, created_by=created_by
            )
            await self.jobs.add(app_id, job)
            await self._audit(
                "kb.document.create",
                app_id,
                actor_id=created_by,
                target_id=doc.id,
                diff={
                    "after": {
                        "title": title,
                        "source_type": source_type,
                        "size_bytes": len(data),
                    }
                },
            )
            await self.session.commit()
        except IntegrityError:
            # 并发上传同一内容：唯一索引 (app_id, content_hash) WHERE deleted_at IS NULL 兜底
            await self.session.rollback()
            if key:
                await self.storage.delete(key)
            found = await self._existing(app_id, digest)
            if found is None:  # pragma: no cover - 冲突的另一方随即被删
                raise
            return found
        except BaseException:
            await self.session.rollback()
            if key:
                await self.storage.delete(key)
            raise
        self._send(INGEST_TASK, [str(doc.id), str(job.id)])
        return UploadResult(doc, False, job)

    async def upload_file(
        self,
        app_id: UUID,
        data: bytes,
        filename: str,
        *,
        title: str | None = None,
        created_by: UUID | None = None,
        chunk_config: Mapping[str, Any] | None = None,
    ) -> UploadResult:
        """FR-KB-01/03：落盘 + 建 job + 投递；同应用 SHA-256 去重，重复返回已有文档（`duplicate=true`）。

        `chunk_config`：文档级切块配置（部分键或 `preset`，合并到应用配置后校验）；重复文件时忽略，
        已有文档的配置用 `set_document_chunk_config` 修改。
        """
        app = await self._app(app_id)
        override = (
            self._resolve_chunk_config(app, chunk_config) if chunk_config is not None else None
        )
        self._check_size(data)
        ftype = detect_file_type(filename, data)  # 扩展名白名单 + 魔数
        name = posixpath.basename(filename.replace("\\", "/")) or f"document.{ftype}"
        return await self._create(
            app_id,
            data,
            ftype,
            title=_clean_title(title, name),
            source_type="file",
            source_uri=None,
            created_by=created_by,
            chunk_config=override,
        )

    async def import_url(
        self,
        app_id: UUID,
        url: str,
        *,
        title: str | None = None,
        created_by: UUID | None = None,
        chunk_config: Mapping[str, Any] | None = None,
    ) -> UploadResult:
        """FR-KB-02：导入时即抓取（SSRF 校验、15s / 10MB 限制），快照存为文件后与上传走同一流程。

        去重按抓取内容的 SHA-256（与文件上传同一规则）；之后的重建使用快照，不重新抓取。
        """
        app = await self._app(app_id)
        override = (
            self._resolve_chunk_config(app, chunk_config) if chunk_config is not None else None
        )
        fetched = await self.fetcher(url)
        self._check_size(fetched.content)
        page_title: str | None = None
        try:
            page_title = parse_bytes(fetched.content, fetched.file_type).title
        except AppError:
            page_title = None  # 正文问题在构建时报告（failed + 原因）
        return await self._create(
            app_id,
            fetched.content,
            fetched.file_type,
            title=_clean_title(title or page_title, fetched.url),
            source_type="url",
            source_uri=url,
            created_by=created_by,
            chunk_config=override,
        )

    # ------------------------------------------------------------------ 替换
    async def _fresh(self, doc: Document) -> Document:
        """返回前重新加载文档：回滚会让实例过期，服务端 `onupdate` 的 `updated_at` 提交后也是过期的，
        而路由是在会话关闭之后才序列化字段（脱离会话再读就是 `DetachedInstanceError`）。"""
        await self.session.refresh(doc)
        return doc

    async def replace_file(
        self,
        app_id: UUID,
        document_id: UUID,
        data: bytes,
        filename: str,
        *,
        created_by: UUID | None = None,
        chunk_config: Mapping[str, Any] | None = None,
        reset_chunk_config: bool = False,
    ) -> ReplaceResult:
        """FR-KB-08（流水线 §5）：文档 ID 不变；新文件保存为 v{version+1}，构建成功后原子切换。

        可同时修改文档级切块配置（`chunk_config` 合并到当前生效配置；`reset_chunk_config` 恢复跟随应用），
        随本次构建一起生效。内容未变但带配置变更时按 `set_document_chunk_config` 处理。
        """
        app = await self._app(app_id)
        self._check_size(data)
        ftype = detect_file_type(filename, data)
        digest = content_hash(data)
        doc = await self._doc(app_id, document_id, lock=True)
        if doc.status in BUSY_STATUSES:
            raise DocumentBusyError()
        override = self._override_payload(app, doc, chunk_config, reset_chunk_config)
        if digest == doc.content_hash:
            if override:
                job = await self._apply_override_locked(app, doc, override, created_by)
                return ReplaceResult(await self._fresh(doc), job is None, job)
            await self.session.rollback()
            return ReplaceResult(await self._fresh(doc), True, None)
        other = await self.docs.get_by_hash(app_id, digest)
        if other is not None and other.id != doc.id:
            raise DuplicateDocumentError(details={"document_id": str(other.id)})
        version = doc.version + 1
        key = document_key(app_id, doc.id, version, ftype)
        n = self._next_build(doc)
        try:
            await self.storage.put(key, data)
            doc.pending_index_version, doc.status, doc.progress, doc.error = n, "queued", 0, None
            job = Job(
                app_id=app_id,
                type="replace",
                target_id=doc.id,
                created_by=created_by,
                payload={
                    "storage_key": key,
                    "content_hash": digest,
                    "mime_type": MIME_TYPES[ftype],
                    "size_bytes": len(data),
                    "version": version,
                    **override,
                },
            )
            await self.jobs.add(app_id, job)
            await self._audit(
                "kb.document.replace",
                app_id,
                actor_id=created_by,
                target_id=doc.id,
                diff={"after": {"version": version, "size_bytes": len(data)}},
            )
            await self.session.commit()
        except BaseException:
            await self.session.rollback()
            await self.storage.delete(key)
            raise
        self._send(INGEST_TASK, [str(doc.id), str(job.id)])
        return ReplaceResult(await self._fresh(doc), False, job)

    # ------------------------------------------------------------------ 重建
    async def _rebuild_locked(
        self,
        app_id: UUID,
        doc: Document,
        created_by: UUID | None,
        payload: Mapping[str, Any] | None = None,
    ) -> Job:
        if doc.status in BUSY_STATUSES:
            raise DocumentBusyError()
        if not doc.storage_key and not doc.source_uri:
            raise ValidationAppError(message="文档没有可重建的来源")
        doc.pending_index_version = self._next_build(doc)
        doc.status, doc.progress, doc.error = "queued", 0, None
        job = Job(
            app_id=app_id,
            type="reindex_document",
            target_id=doc.id,
            payload=dict(payload or {}),
            created_by=created_by,
        )
        await self.jobs.add(app_id, job)
        return job

    async def rebuild_document(
        self,
        app_id: UUID,
        document_id: UUID,
        *,
        created_by: UUID | None = None,
        chunk_config: Mapping[str, Any] | None = None,
        reset_chunk_config: bool = False,
    ) -> Job:
        """单文档重建（含失败重试）：新构建号重新解析、切块、向量化；构建期间旧版本继续可检索。

        可带文档级切块配置变更（同 `replace_file`），构建成功切换时生效。
        """
        app = await self._app(app_id)
        doc = await self._doc(app_id, document_id, lock=True)
        override = self._override_payload(app, doc, chunk_config, reset_chunk_config)
        job = await self._rebuild_locked(app_id, doc, created_by, override)
        await self._audit(
            "kb.document.reindex",
            app_id,
            actor_id=created_by,
            target_id=doc.id,
            diff={"after": {"build": doc.pending_index_version}},
        )
        await self.session.commit()
        self._send(INGEST_TASK, [str(doc.id), str(job.id)])
        return job

    async def _apply_override_locked(
        self, app: App, doc: Document, override: Mapping[str, Any], created_by: UUID | None
    ) -> Job | None:
        """生效切块配置变了 → 只重建本文档（配置随构建生效）；只是“固定 / 跟随”标记变化 → 直接写入。"""
        if doc.status in BUSY_STATUSES:
            raise DocumentBusyError()
        new = override[OVERRIDE_KEY]
        if (new or app.chunk_config) == (doc.chunk_config or app.chunk_config):
            changed = new != doc.chunk_config
            if changed:
                doc.chunk_config = new
            await self.session.commit()
            await self._fresh(doc)
            return None
        job = await self._rebuild_locked(app.id, doc, created_by, override)
        await self.session.commit()
        await self._fresh(doc)
        self._send(INGEST_TASK, [str(doc.id), str(job.id)])
        return job

    async def set_document_chunk_config(
        self,
        app_id: UUID,
        document_id: UUID,
        chunk_config: Mapping[str, Any] | None,
        *,
        created_by: UUID | None = None,
    ) -> Job | None:
        """修改文档级切块配置（`None` = 恢复跟随应用）。生效配置变化时只重建该文档并返回任务；
        未变化返回 None。新配置在构建成功切换时写入（构建失败则保持原配置）。"""
        app = await self._app(app_id)
        doc = await self._doc(app_id, document_id, lock=True)
        override = self._override_payload(app, doc, chunk_config, chunk_config is None)
        return await self._apply_override_locked(app, doc, override, created_by)

    async def rebuild_app(
        self, app_id: UUID, *, created_by: UUID | None = None, include_overridden: bool = False
    ) -> RebuildAppResult:
        """应用全部文档重建（`chunk_config` 变化后一键重建）；构建中的文档跳过并在结果中列出。

        有文档级 chunk_config 覆盖的文档不受应用配置影响，默认不重建（列入 `skipped_override`）；
        `include_overridden=True`（如更换嵌入模型后全量重建）时一并按各自配置重建。
        """
        await self._app(app_id)
        result = RebuildAppResult(
            job=Job(app_id=app_id, type="reindex_app", target_id=app_id, created_by=created_by)
        )
        for d in await self.docs.list_not_deleting(app_id):
            doc = await self.docs.get_for_update(app_id, d.id)
            if (
                doc is None
                or doc.status in BUSY_STATUSES
                or not (doc.storage_key or doc.source_uri)
            ):
                result.skipped.append(d.id)
                continue
            if doc.chunk_config is not None and not include_overridden:
                result.skipped_override.append(d.id)
                continue
            result.jobs.append(await self._rebuild_locked(app_id, doc, created_by))
        result.job.status, result.job.progress, result.job.finished_at = "done", 100, _now()
        result.job.payload = {
            "document_jobs": [str(j.id) for j in result.jobs],
            "skipped": [str(i) for i in result.skipped],
            "skipped_override": [str(i) for i in result.skipped_override],
        }
        await self.jobs.add(app_id, result.job)
        await self.session.commit()
        for j in result.jobs:
            self._send(INGEST_TASK, [str(j.target_id), str(j.id)])
        return result

    # ------------------------------------------------------------------ 标签（M2.2b）
    async def set_document_tags(
        self, app_id: UUID, document_id: UUID, tags: Sequence[str]
    ) -> Document:
        """Replace the document's tags (PG first, then Qdrant payload of all its points).

        A failed Qdrant sync is logged, not raised: retrieval re-checks tags in PG
        (`fetch_chunks(tags=...)`), so a stale payload can only miss a hit, never leak one
        across tags; the next edit or rebuild re-mirrors."""
        clean = normalize_tags(tags)
        await self._app(app_id)
        doc = await self._doc(app_id, document_id, lock=True)
        if doc.status == "deleting":
            raise DocumentBusyError(message="文档正在删除")
        doc.tags = clean
        await self.session.commit()
        if self.store is not None:
            try:
                await self.store.set_document_tags(app_id, doc.id, clean)
            except Exception:  # noqa: BLE001
                log.warning("knowledge.tag_sync_failed", document_id=str(doc.id))
        return await self._fresh(doc)

    # ------------------------------------------------------------------ 补充问题（M2.2b）
    async def _live_chunk(self, app_id: UUID, chunk_id: UUID) -> tuple[Chunk, Document]:
        row = (
            await self.session.execute(
                select(Chunk, Document)
                .join(Document, Document.id == Chunk.document_id)
                .where(
                    Chunk.app_id == app_id,
                    Chunk.id == chunk_id,
                    Chunk.index_version == Document.index_version,
                    Document.deleted_at.is_(None),
                )
            )
        ).first()
        if row is None:
            raise ChunkNotFoundError(details={"chunk_id": str(chunk_id)})
        chunk, doc = row
        if doc.status == "deleting":
            raise DocumentBusyError(message="文档正在删除")
        return chunk, doc

    def _need_vectors(self) -> tuple[EmbeddingProvider, QdrantVectorStore]:
        if self.embedder is None or self.store is None:
            raise RuntimeError("KnowledgeService needs embedder and store for questions")
        return self.embedder, self.store

    @staticmethod
    def _target(chunk: Chunk, doc: Document) -> QuestionTarget:
        return QuestionTarget(
            chunk_id=chunk.id,
            document_id=doc.id,
            index_version=chunk.index_version,
            parent_id=chunk.parent_id,
            enabled=chunk.enabled and chunk.review_status != "flagged",
            tags=list(doc.tags or []),
        )

    async def add_chunk_questions(
        self,
        app_id: UUID,
        chunk_id: UUID,
        questions: Sequence[str],
        *,
        source: str = "manual",
        created_by: UUID | None = None,
    ) -> list[ChunkQuestion]:
        """Attach questions to a live chunk; each is embedded as its own point.

        Order: rows flushed → points upserted → commit; an upsert failure rolls back the
        rows, a commit failure deletes the new points (best effort)."""
        if source not in QUESTION_SOURCES:
            raise ValidationAppError(message=f"source 必须是 {'/'.join(QUESTION_SOURCES)}")
        clean = normalize_questions(questions)
        embedder, store = self._need_vectors()
        await self._app(app_id)
        chunk, doc = await self._live_chunk(app_id, chunk_id)
        existing = {q.question for q in await self.questions.list_for_chunk(app_id, chunk.id)}
        new = [q for q in clean if q not in existing]
        if len(existing) + len(new) > MAX_QUESTIONS_PER_CHUNK:
            raise ValidationAppError(message=f"每个切块最多 {MAX_QUESTIONS_PER_CHUNK} 个补充问题")
        if not new:
            return []
        rows = [
            ChunkQuestion(
                app_id=app_id,
                document_id=doc.id,
                chunk_id=chunk.id,
                question=q,
                source=source,
                anchor_ordinal=chunk.ordinal,
                anchor_hash=text_hash(chunk.text),
                created_by=created_by,
            )
            for q in new
        ]
        for r in rows:
            await self.questions.add(app_id, r)
        target = self._target(chunk, doc)
        try:
            await upsert_question_points(
                app_id, [(r.id, r.question) for r in rows], target, embedder=embedder, store=store
            )
        except Exception:
            await self.session.rollback()
            raise
        try:
            await self.session.commit()
        except Exception:
            await store.delete_points(app_id, [r.id for r in rows])
            raise
        return rows

    async def list_chunk_questions(self, app_id: UUID, chunk_id: UUID) -> list[ChunkQuestion]:
        await self._app(app_id)
        return await self.questions.list_for_chunk(app_id, chunk_id)

    async def list_document_questions(
        self, app_id: UUID, document_id: UUID, *, status: str | None = None
    ) -> list[ChunkQuestion]:
        await self._app(app_id)
        await self._doc(app_id, document_id)
        return await self.questions.list_for_document(app_id, document_id, status=status)

    async def delete_chunk_question(self, app_id: UUID, question_id: UUID) -> None:
        await self._app(app_id)
        if not await self.questions.delete_one(app_id, question_id):
            raise QuestionNotFoundError()
        await self.session.commit()
        if self.store is not None:
            try:
                await self.store.delete_points(app_id, [question_id])
            except Exception:  # noqa: BLE001 - orphan point maps to a chunk; harmless, logged
                log.warning("knowledge.question_point_delete_failed", question_id=str(question_id))

    async def reattach_chunk_question(
        self, app_id: UUID, question_id: UUID, chunk_id: UUID
    ) -> ChunkQuestion:
        """Point a (typically pending) question at a live chunk and re-embed it."""
        embedder, store = self._need_vectors()
        await self._app(app_id)
        q = await self.questions.get(app_id, question_id)
        if q is None:
            raise QuestionNotFoundError()
        chunk, doc = await self._live_chunk(app_id, chunk_id)
        q.chunk_id, q.document_id, q.status = chunk.id, doc.id, "active"
        q.anchor_ordinal, q.anchor_hash = chunk.ordinal, text_hash(chunk.text)
        await self.session.flush()
        try:
            await upsert_question_points(
                app_id,
                [(q.id, q.question)],
                self._target(chunk, doc),
                embedder=embedder,
                store=store,
            )
        except Exception:
            await self.session.rollback()
            raise
        await self.session.commit()
        return q

    # ------------------------------------------------------------------ 命中测试（M2.2b）
    async def hit_test(
        self,
        app_id: UUID,
        query: str,
        *,
        reranker: RerankProvider | None,
        overrides: Mapping[str, Any] | None = None,
        filters: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Run retrieve → fuse → rerank only (RAG 流程设计 §4.2): no LLM, no conversation,
        no message or statistics writes. `overrides = {"rag_config": {...}}` is merged over
        the app's rag_config and validated like a save; `filters = {"tags": [...]}`."""
        q = (query or "").strip()
        if not q:
            raise ValidationAppError(message="query 不能为空")
        if len(q) > HIT_TEST_MAX_CHARS:
            raise ValidationAppError(message=f"query 最多 {HIT_TEST_MAX_CHARS} 字")
        unknown = set(overrides or {}) - {"rag_config"}
        if unknown:
            raise ValidationAppError(message=f"不支持的 overrides：{', '.join(sorted(unknown))}")
        bad_filters = set(filters or {}) - {"tags"}
        if bad_filters:
            raise ValidationAppError(message=f"不支持的 filters：{', '.join(sorted(bad_filters))}")
        embedder, store = self._need_vectors()
        app = await self._app(app_id)
        merged = {**(app.rag_config or {}), **dict((overrides or {}).get("rag_config") or {})}
        try:
            cfg = AppRagConfig.model_validate(merged)
        except ValidationError as e:
            raise ValidationAppError(details={"errors": e.errors(include_url=False)}) from e
        tags = normalize_tags((filters or {})["tags"]) if filters and "tags" in filters else None
        res = await run_retrieval(
            app_id,
            q,
            cfg,
            vector=EmbeddingVectorSearch(embedder, store),
            chunk_search=PgChunkSearch(
                async_sessionmaker(self.session.bind, expire_on_commit=False)
            ),
            reranker=reranker,
            tags=tags,
        )

        def unit(c: Candidate) -> dict[str, Any]:
            return {
                **c.trace(),
                "document_title": c.chunk.document_title,
                "heading_path": c.heading_path,
                "text": c.text[:HIT_TEST_PREVIEW_CHARS],
            }

        return {
            "query": q,
            "rag_config": cfg.model_dump(
                include={
                    "retrieval_mode",
                    "fusion",
                    "vector_weight",
                    "vector_score_threshold",
                    "vector_top_k",
                    "keyword_top_k",
                    "fuse_top_k",
                    "rerank_top_n",
                    "enable_rerank",
                    "rerank_threshold",
                }
            ),
            "tags": res.tags,
            "vector": [
                {"chunk_id": str(h.chunk_id), "score": h.score, "rank": h.rank}
                for h in res.vector_hits
            ],
            "keyword": [
                {"chunk_id": str(h.chunk_id), "score": h.score, "rank": h.rank}
                for h in res.keyword_hits
            ],
            "fused": [unit(c) for c in res.fused],
            "reranked": [unit(c) for c in res.reranked],
            "rerank_applied": res.rerank_applied,
            "degraded": res.degraded,
            "timings": res.timings,
        }

    # ------------------------------------------------------------------ 删除
    async def delete_document(
        self, app_id: UUID, document_id: UUID, *, created_by: UUID | None = None
    ) -> Job:
        """流水线 §6：deleting + deleted_at，切块立即不可检索，再投递 cleanup_document。

        构建中的文档也可删除：构建任务在下一次中止检查时停止（job cancelled）。
        """
        await self._app(app_id)
        doc = await self._doc(app_id, document_id, lock=True)
        if doc.status == "deleting":
            raise DocumentBusyError(message="文档正在删除")
        doc.status, doc.deleted_at = "deleting", _now()
        await self.chunks.disable_document(app_id, doc.id)
        job = Job(
            app_id=app_id,
            type="cleanup_document",
            target_id=doc.id,
            payload={},
            created_by=created_by,
        )
        await self.jobs.add(app_id, job)
        await self._audit(
            "kb.document.delete",
            app_id,
            actor_id=created_by,
            target_id=doc.id,
            diff={"before": {"title": doc.title, "status": doc.status}},
        )
        await self.session.commit()
        if self.store is not None:
            # 尽力立即同步 payload；失败不影响删除（PG 已不可检索，cleanup 会删点）
            try:
                await self.store.set_document_enabled(app_id, doc.id, False)
            except Exception:  # noqa: BLE001
                log.warning("knowledge.disable_points_failed", document_id=str(doc.id))
        self._send(CLEANUP_TASK, [str(doc.id), None, None, str(job.id)])
        return job

    # ------------------------------------------------------------------ 重新分词
    async def request_retokenize(self, app_id: UUID, *, created_by: UUID | None = None) -> Job:
        """`custom_dict` 变化后调用（`AppService.update_app` 返回 `custom_dict_changed`）。"""
        await self._app(app_id)
        job = Job(
            app_id=app_id,
            type="retokenize_app",
            target_id=app_id,
            payload={},
            created_by=created_by,
        )
        await self.jobs.add(app_id, job)
        await self.session.commit()
        self._send(RETOKENIZE_TASK, [str(app_id), str(job.id)])
        return job

    # ------------------------------------------------------------------ 对账
    async def request_reconcile(
        self, app_id: UUID | None = None, *, created_by: UUID | None = None
    ) -> Job:
        """手动对账（Qdrant 设计 §5）：写 jobs(type=reconcile)；app_id 为空时为平台级任务。"""
        job = Job(
            app_id=app_id, type="reconcile", target_id=app_id, payload={}, created_by=created_by
        )
        if app_id is None:
            await self.jobs.add_platform_job_all_apps(job)
        else:
            await self._app(app_id)
            await self.jobs.add(app_id, job)
        await self.session.commit()
        self._send(RECONCILE_TASK, [str(app_id) if app_id else None, str(job.id)])
        return job

    # ------------------------------------------------------------------ 开放接口：读
    async def list_documents(
        self,
        app_id: UUID,
        *,
        status: str | None = None,
        q: str | None = None,
        page: int = 1,
        page_size: int = PAGE_DEFAULT,
    ) -> DocumentPage:
        """文档列表（开放接口 §4）：按创建时间倒序，`q` 在标题上 ILIKE（通配符转义），软删除不返回。"""
        await self._app(app_id)
        p, size, offset = page_args(page, page_size)
        rows, total = await self.docs.page_for_app(
            app_id, status=status, q=(q or "").strip() or None, limit=size, offset=offset
        )
        return DocumentPage(items=rows, total=total, page=p, page_size=size)

    async def get_document(self, app_id: UUID, document_id: UUID) -> Document:
        """单篇详情（开放接口 §4）：不存在或不属于本应用 → 404 `DOCUMENT_NOT_FOUND`。"""
        await self._app(app_id)
        return await self._doc(app_id, document_id)

    async def list_chunks(
        self,
        app_id: UUID,
        document_id: UUID,
        *,
        page: int = 1,
        page_size: int = PAGE_DEFAULT,
    ) -> ChunkPage:
        """切块列表（开放接口 §4）：只返回生效构建号（`index_version`）的切块，按 `ordinal` 升序。"""
        await self._app(app_id)
        doc = await self._doc(app_id, document_id)
        p, size, offset = page_args(page, page_size)
        if doc.index_version <= 0:
            return ChunkPage(items=[], total=0, page=p, page_size=size)
        rows, total = await self.chunks.page_for_document(
            app_id, doc.id, doc.index_version, limit=size, offset=offset
        )
        return ChunkPage(items=rows, total=total, page=p, page_size=size)

    # ------------------------------------------------------------------ 管理版切块（M4.3 ③）
    async def list_app_chunks(
        self,
        app_id: UUID,
        *,
        document_id: UUID | None = None,
        review_status: str | None = None,
        enabled: bool | None = None,
        page: int = 1,
        page_size: int = PAGE_DEFAULT,
    ) -> AppChunkPage:
        """跨文档切块列表（管理接口 §知识库）：待复核 = `review_status=flagged`。"""
        await self._app(app_id)
        p, size, offset = page_args(page, page_size)
        rows, total = await self.chunks.page_for_app(
            app_id,
            document_id=document_id,
            review_status=review_status,
            enabled=enabled,
            limit=size,
            offset=offset,
        )
        items = [
            {
                "id": chunk.id,
                "document_id": chunk.document_id,
                "document_title": title,
                "ordinal": chunk.ordinal,
                "heading_path": chunk.heading_path,
                "text": chunk.text,
                "tokens": chunk.tokens,
                "enabled": chunk.enabled,
                "review_status": chunk.review_status,
                "flag_reason": chunk.flag_reason,
                "reviewed_at": chunk.reviewed_at,
            }
            for chunk, title in rows
        ]
        return AppChunkPage(items=items, total=total, page=p, page_size=size)

    async def update_chunk(
        self,
        app_id: UUID,
        chunk_id: UUID,
        *,
        enabled: bool | None = None,
        review_status: str | None = None,
        text: str | None = None,
        heading_path: str | None = None,
        created_by: UUID | None = None,
    ) -> Chunk:
        """切块启停、复核与正文编辑（管理接口 §知识库）；正文变化时只重算该块向量。

        先提交数据库、再同步 Qdrant payload；向量库失败只记日志——重试本接口幂等。
        """
        chunk, doc = await self._live_chunk(app_id, chunk_id)
        before: dict[str, Any] = {}
        after: dict[str, Any] = {}
        if enabled is not None and chunk.enabled != enabled:
            before["enabled"], after["enabled"] = chunk.enabled, enabled
            chunk.enabled = enabled
        if review_status is not None:
            if review_status != REVIEW_APPROVED:
                raise ValidationAppError(message=f"review_status 只接受 {REVIEW_APPROVED}")
            before["review_status"] = chunk.review_status
            after["review_status"] = review_status
            chunk.review_status = review_status
            chunk.reviewed_by = created_by
            chunk.reviewed_at = _now()
            if not chunk.enabled:
                before.setdefault("enabled", chunk.enabled)
                after["enabled"] = True
                chunk.enabled = True
        content_changed = text is not None or heading_path is not None
        if content_changed:
            app = await self._app(app_id)
            new_text = chunk.text if text is None else text.strip()
            if text is not None and not new_text:
                raise ValidationAppError(
                    message="切块正文不能为空",
                    details={"errors": [{"loc": ["text"], "msg": "不能为空"}]},
                )
            new_heading = (
                chunk.heading_path if heading_path is None else (heading_path.strip() or None)
            )
            content_changed = new_text != chunk.text or new_heading != chunk.heading_path
            if content_changed:
                before.update({"text": chunk.text, "heading_path": chunk.heading_path})
                chunk.text = new_text
                chunk.heading_path = new_heading
                chunk.search_text = search_text_for(
                    app_id, new_heading, new_text, list(app.custom_dict or [])
                )
                chunk.tokens = count_tokens(new_text)
                chunk.source = CHUNK_EDITED
                after.update(
                    {
                        "text": new_text,
                        "heading_path": new_heading,
                        "source": CHUNK_EDITED,
                    }
                )
        if not after:
            return chunk
        await self._audit(
            "kb.chunk.update",
            app_id,
            actor_id=created_by,
            target_id=chunk.id,
            diff={"before": before, "after": after},
            target_type="chunk",
        )
        await self.session.commit()
        await self.session.refresh(chunk)
        await self._sync_chunk_payload(app_id, chunk)
        if content_changed:
            await self._reembed_chunk(app_id, chunk, doc)
        return chunk

    async def add_chunk(
        self,
        app_id: UUID,
        document_id: UUID,
        *,
        text: str,
        heading_path: str | None = None,
        created_by: UUID | None = None,
    ) -> Chunk:
        """手工新增切块（`source="manual"`）：只向量化这一块，重建时会被迁移到新构建号。"""
        app = await self._app(app_id)
        doc = await self._doc(app_id, document_id, lock=True)
        body = text.strip()
        if not body:
            raise ValidationAppError(
                message="切块正文不能为空",
                details={"errors": [{"loc": ["text"], "msg": "不能为空"}]},
            )
        heading = (heading_path or "").strip() or None
        next_ordinal = await self.session.scalar(
            select(func.coalesce(func.max(Chunk.ordinal), -1) + 1).where(
                Chunk.app_id == app_id,
                Chunk.document_id == document_id,
                Chunk.index_version == doc.index_version,
            )
        )
        chunk = Chunk(
            app_id=app_id,
            document_id=document_id,
            index_version=doc.index_version,
            ordinal=int(next_ordinal or 0),
            heading_path=heading,
            text=body,
            search_text=search_text_for(app_id, heading, body, list(app.custom_dict or [])),
            tokens=count_tokens(body),
            enabled=True,
            source=CHUNK_MANUAL,
        )
        self.session.add(chunk)
        await self.session.flush()
        await self._audit(
            "kb.chunk.create",
            app_id,
            actor_id=created_by,
            target_id=chunk.id,
            diff={"after": {"text": body, "heading_path": heading, "source": CHUNK_MANUAL}},
            target_type="chunk",
        )
        await self.session.commit()
        await self.session.refresh(chunk)
        await self._reembed_chunk(app_id, chunk, doc)
        return chunk

    async def delete_chunk(
        self, app_id: UUID, chunk_id: UUID, *, created_by: UUID | None = None
    ) -> None:
        """删除单个切块：先删向量点再删行；向量库失败只记日志（行删除仍然生效）。"""
        chunk, _doc = await self._live_chunk(app_id, chunk_id)
        before = {"text": chunk.text, "heading_path": chunk.heading_path, "source": chunk.source}
        if self.store is not None:
            try:
                await self.store.delete_points(app_id, [chunk.id])
            except Exception:
                log.warning(
                    "knowledge.chunk_delete_points_failed",
                    app_id=str(app_id),
                    chunk_id=str(chunk.id),
                )
        await self.session.delete(chunk)
        await self._audit(
            "kb.chunk.delete",
            app_id,
            actor_id=created_by,
            target_id=chunk_id,
            diff={"before": before},
            target_type="chunk",
        )
        await self.session.commit()

    async def _reembed_chunk(self, app_id: UUID, chunk: Chunk, doc: Document) -> None:
        """单点重算向量与 payload（编辑 / 手工新增），其余切块不受影响。"""
        if self.store is None or self.embedder is None:
            return
        cfg = dict(doc.chunk_config or {})
        if not cfg:
            cfg = dict((await self._app(app_id)).chunk_config or {})
        text = build_embed_text(
            doc.indexed_title or doc.title,
            chunk.heading_path,
            chunk.text,
            contextual_prefix=bool(cfg.get("contextual_prefix", True)),
        )
        try:
            await embed_and_upsert(
                app_id=app_id,
                document_id=doc.id,
                index_version=chunk.index_version,
                items=[EmbedItem(chunk.id, chunk.parent_id, text, enabled=chunk.enabled)],
                embedder=self.embedder,
                store=self.store,
                batch_size=1,
                tags=list(doc.tags or []),
            )
        except Exception:
            log.warning(
                "knowledge.chunk_reembed_failed", app_id=str(app_id), chunk_id=str(chunk.id)
            )

    async def duplicates(self, app_id: UUID, document_id: UUID) -> list[dict[str, Any]]:
        """近重复提示：同应用其他未删除文档中 SimHash 海明距离 ≤ 3 的（只提示不阻止）。"""
        doc = await self._doc(app_id, document_id)
        if doc.simhash is None:
            return []
        own = to_unsigned(int(doc.simhash))
        rows = (
            await self.session.execute(
                select(Document.id, Document.title, Document.simhash).where(
                    Document.app_id == app_id,
                    Document.id != document_id,
                    Document.deleted_at.is_(None),
                    Document.simhash.is_not(None),
                )
            )
        ).all()
        found = []
        for row in rows:
            if row[2] is None:  # 理论上 SQL 已过滤，防御历史脏数据
                continue
            other = to_unsigned(int(row[2]))
            if near_duplicate(own, other):
                found.append(
                    {
                        "document_id": str(row[0]),
                        "title": row[1],
                        "distance": hamming(own, other),
                    }
                )
        found.sort(key=lambda item: item["distance"])
        return found

    async def _sync_chunk_payload(self, app_id: UUID, chunk: Chunk) -> None:
        """把 `enabled` 同步到 Qdrant payload（检索按 payload 过滤）。失败只记日志。"""
        if self.store is None:
            return
        try:
            await self.store.set_enabled(app_id, [chunk.id], chunk.enabled)
        except Exception:
            log.warning(
                "knowledge.chunk_payload_sync_failed",
                app_id=str(app_id),
                chunk_id=str(chunk.id),
            )

    # ------------------------------------------------------------------ 开放接口：写
    async def update_document(
        self,
        app_id: UUID,
        document_id: UUID,
        *,
        title: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        effective_at: datetime | None | object = KEEP,
        expires_at: datetime | None | object = KEEP,
        created_by: UUID | None = None,
    ) -> Document:
        """改标题 / 元数据 / 有效期（开放接口 §4 `PATCH`）：立即生效，无需重建。"""
        await self._app(app_id)
        if title is not None and not title.strip():
            raise ValidationAppError(message="标题不能为空")
        if metadata is not None:
            body = json.dumps(dict(metadata), ensure_ascii=False, default=str)
            if len(body.encode()) > METADATA_MAX_BYTES:
                raise ValidationAppError(message=f"metadata 超过 {METADATA_MAX_BYTES} 字节")
        doc = await self._doc(app_id, document_id, lock=True)
        before = {
            "title": doc.title,
            "metadata": dict(doc.metadata_ or {}),
            "effective_at": _iso_or_none(doc.effective_at),
            "expires_at": _iso_or_none(doc.expires_at),
        }
        if title is not None:
            doc.title = _clean_title(title, doc.title)
        if metadata is not None:
            doc.metadata_ = dict(metadata)
        if effective_at is not KEEP or expires_at is not KEEP:
            new_effective = doc.effective_at if effective_at is KEEP else effective_at
            new_expires = doc.expires_at if expires_at is KEEP else expires_at
            if (
                new_effective is not None
                and new_expires is not None
                and new_effective >= new_expires
            ):
                raise ValidationAppError(
                    message="生效时间必须早于失效时间",
                    details={"errors": [{"loc": ["expires_at"], "msg": "必须晚于 effective_at"}]},
                )
            doc.effective_at = new_effective
            doc.expires_at = new_expires
        after = {
            "title": doc.title,
            "metadata": dict(doc.metadata_ or {}),
            "effective_at": _iso_or_none(doc.effective_at),
            "expires_at": _iso_or_none(doc.expires_at),
        }
        await self._audit(
            "kb.document.update",
            app_id,
            actor_id=created_by,
            target_id=doc.id,
            diff={
                "before": {k: before[k] for k in before if before[k] != after[k]},
                "after": {k: after[k] for k in after if before[k] != after[k]},
            },
        )
        await self.session.commit()
        # updated_at 由服务端 onupdate 生成，提交后该列是 expired 的；脱离 session 再读会
        # DetachedInstanceError（PATCH 应答正好要序列化它），所以在会话内刷新一次。
        await self.session.refresh(doc)
        return doc

    # ------------------------------------------------------------------ 查询
    async def retrievable_chunk_count(self, app_id: UUID) -> int:
        """对账与测试用的可检索切块数：复用 `chunk_search.retrievable()`，不另写一份条件。"""
        from aics.db.models import Chunk

        stmt = (
            select(func.count())
            .select_from(Chunk)
            .join(Document, Document.id == Chunk.document_id)
            .where(Chunk.app_id == app_id, *retrievable())
        )
        return int((await self.session.execute(stmt)).scalar_one())
