"""knowledge 服务：上传（去重）、URL 导入、替换、删除、重建、重新分词（文档处理流水线 §3.3–§6）。

M1 阶段只有 service 与 `aics-cli`，HTTP 路由随 M3（开放接口 `/api/v1/apps/{app_id}/documents`）
与 M4（后台）接入；路由层把这里的异常映射为接口规范中的错误码。
任务按任务名投递（`enqueue(task_name, args)`），本模块不 import `aics.tasks`。
"""

from __future__ import annotations

import hashlib
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
from sqlalchemy.ext.asyncio import AsyncSession

from aics.core.config import get_settings
from aics.core.errors import AppError, NotFoundError, ValidationAppError
from aics.db.models import App, Document, Job
from aics.db.models.knowledge import MAX_TAGS, TAG_MAX_CHARS
from aics.ingest.parsers import MIME_TYPES, detect_file_type, parse_bytes
from aics.ingest.parsers.url import FetchedUrl, fetch_url
from aics.ingest.service import OVERRIDE_KEY
from aics.modules.knowledge.repository import ChunkRepository, DocumentRepository, JobRepository
from aics.rag.config import (
    AppRagConfig,
    ChunkConfig,
    ConfigValidationError,
    check_chunk_window,
    merge_chunk_config,
)
from aics.storage import Storage
from aics.storage.base import document_key
from aics.vectorstore import QdrantVectorStore

log = structlog.get_logger()

INGEST_TASK = "aics.ingest_document"
CLEANUP_TASK = "aics.cleanup_document"
RETOKENIZE_TASK = "aics.retokenize_app"
RECONCILE_TASK = "aics.reconcile"
TITLE_MAX_CHARS = 200
BUSY_STATUSES = frozenset({"queued", "parsing", "indexing", "deleting"})

Enqueue = Callable[[str, Sequence[Any]], None]
Fetcher = Callable[[str], Awaitable[FetchedUrl]]


class DocumentBusyError(AppError):
    code, status, message = "DOCUMENT_BUSY", 409, "文档正在构建或删除中，请稍后再试"


class DuplicateDocumentError(AppError):
    code, status, message = "DUPLICATE_DOCUMENT", 409, "新文件与本应用另一文档内容相同"


class DocumentNotFoundError(NotFoundError):
    code, message = "DOCUMENT_NOT_FOUND", "文档不存在"


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
    ) -> None:
        self.session = session
        self.storage = storage
        self.store = store
        self.enqueue = enqueue
        self.fetcher = fetcher or self._fetch
        self.docs = DocumentRepository(session)
        self.chunks = ChunkRepository(session)
        self.jobs = JobRepository(session)

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
            raise DocumentNotFoundError()
        return doc

    def _send(self, task: str, args: Sequence[Any]) -> None:
        if self.enqueue is not None:
            self.enqueue(task, list(args))

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
            raise ValidationAppError(message=f"文件超过 {max_bytes // (1024 * 1024)} MB")

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
                return ReplaceResult(doc, job is None, job)
            await self.session.rollback()
            return ReplaceResult(doc, True, None)
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
            await self.session.commit()
        except BaseException:
            await self.session.rollback()
            await self.storage.delete(key)
            raise
        self._send(INGEST_TASK, [str(doc.id), str(job.id)])
        return ReplaceResult(doc, False, job)

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
            return None
        job = await self._rebuild_locked(app.id, doc, created_by, override)
        await self.session.commit()
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
        return doc

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

    # ------------------------------------------------------------------ 查询
    async def retrievable_chunk_count(self, app_id: UUID) -> int:
        """数据库设计“可检索条件”的唯一 SQL 实现（对账与测试使用）。"""
        from aics.db.models import Chunk

        stmt = (
            select(func.count())
            .select_from(Chunk)
            .join(Document, Document.id == Chunk.document_id)
            .where(
                Chunk.app_id == app_id,
                Chunk.enabled,
                Chunk.review_status != "flagged",
                Chunk.index_version == Document.index_version,
                Document.status != "deleting",
            )
        )
        return int((await self.session.execute(stmt)).scalar_one())
