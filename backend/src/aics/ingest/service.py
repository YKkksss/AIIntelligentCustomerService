"""入库流水线 service（文档处理流水线 §3、§3.1、§6）。

- `preview` / `preview_document`：切块预览，只解析 + 切块，**不**向量化、不写库（M1.4）。
- `run_build`：一次构建（解析 → 切块 → 写父块/切块 → 向量化 → 原子切换生效版本）。
- `run_cleanup`：删除旧版本（或已删除文档）的点、切块、父块与旧文件。

Celery 任务（`aics.tasks.ingest` / `aics.tasks.maintenance`）只调用 `run_ingest_job` / `run_cleanup_job`：
每个任务在自己的事件循环里新建并释放连接（`IngestRuntime`），不复用 API 进程的全局引擎。
"""

from __future__ import annotations

import logging
import posixpath
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from aics.core.config import Settings, get_settings
from aics.core.errors import AppError
from aics.core.textsearch import tokenize_for_index
from aics.db.base import new_id
from aics.db.models import App, Chunk, ChunkParent, Document, Job
from aics.ingest.chunker import ChunkOptions, ChunkResult, split_document
from aics.ingest.indexer import EmbedItem, build_embed_text, embed_and_upsert
from aics.ingest.parsers import detect_file_type, parse_bytes
from aics.ingest.parsers.base import ParsedDoc, ParseError
from aics.ingest.parsers.url import fetch_url
from aics.ingest.repository import ChunkParentRepository, ChunkRepository
from aics.providers.base import EmbeddingProvider
from aics.storage import Storage, build_storage
from aics.vectorstore import QdrantVectorStore, build_vector_store

log = logging.getLogger(__name__)

PREVIEW_DEFAULT_LIMIT = 50
PREVIEW_MAX_LIMIT = 200
PROGRESS_SPLIT_DONE = 30  # 解析 + 切块完成；30→100 为向量化
EMBED_PROGRESS_BATCHES = 4  # 每个进度批包含 embed_batch × 4 条（嵌入器内部再按 embed_batch 拆）

CleanupEnqueuer = Callable[[UUID, int, str | None], Awaitable[None] | None]


class DocumentNotFoundError(AppError):
    code = "NOT_FOUND"
    status = 404
    message = "文档不存在"


class _Cancelled(Exception):
    pass


def _now() -> datetime:
    return datetime.now(UTC)


def _embed_text_for(
    title: str | None, chunk_config: Mapping[str, Any], heading: str, text: str
) -> str:
    return build_embed_text(
        title, heading, text, contextual_prefix=bool(chunk_config.get("contextual_prefix", True))
    )


def _clamp_limit(limit: int) -> int:
    return max(1, min(int(limit), PREVIEW_MAX_LIMIT))


def preview_result(
    result: ChunkResult, *, title: str | None, chunk_config: Mapping[str, Any], limit: int
) -> dict[str, Any]:
    """预览返回结构（文档处理流水线 §3.1）；`embed_text` 与构建时完全同一函数生成。"""
    n = _clamp_limit(limit)
    return {
        "total_chunks": len(result.chunks),
        "total_parents": len(result.parents),
        "total_tokens": sum(c.tokens for c in result.chunks),
        "chunks": [
            {
                "ordinal": c.ordinal,
                "heading_path": c.heading_path,
                "text": c.text,
                "tokens": c.tokens,
                "parent_ordinal": c.parent_ordinal,
                "embed_text": _embed_text_for(title, chunk_config, c.heading_path, c.text),
            }
            for c in result.chunks[:n]
        ],
        "parents": [
            {
                "ordinal": p.ordinal,
                "heading_path": p.heading_path,
                "tokens": p.tokens,
                "text": p.text,
            }
            for p in result.parents[:n]
        ],
    }


def _ext(key: str | None) -> str:
    return posixpath.basename(key or "") or "document"


class IngestService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        storage: Storage,
        store: QdrantVectorStore,
        embedder: EmbeddingProvider,
        settings: Settings,
        enqueue_cleanup: CleanupEnqueuer | None = None,
    ) -> None:
        self.sessions = sessions
        self.storage = storage
        self.store = store
        self.embedder = embedder
        self.settings = settings
        self.enqueue_cleanup = enqueue_cleanup

    # ------------------------------------------------------------------ 预览
    async def preview(
        self,
        data: bytes,
        filename: str,
        chunk_config: Mapping[str, Any],
        *,
        title: str | None = None,
        limit: int = PREVIEW_DEFAULT_LIMIT,
    ) -> dict[str, Any]:
        """上传前预览。`chunk_config` 须已由调用方（knowledge service）用 `ChunkConfig` 校验。"""
        parsed = parse_bytes(data, detect_file_type(filename, data))
        result = split_document(parsed, ChunkOptions.from_mapping(chunk_config))
        return preview_result(
            result, title=title or parsed.title or filename, chunk_config=chunk_config, limit=limit
        )

    async def preview_document(
        self,
        app_id: UUID,
        document_id: UUID,
        chunk_config: Mapping[str, Any] | None = None,
        *,
        limit: int = PREVIEW_DEFAULT_LIMIT,
    ) -> dict[str, Any]:
        """已有文档按新配置（缺省为应用当前配置）预览，用于改配置前比较。"""
        async with self.sessions() as s:
            doc = await s.get(Document, document_id)
            if doc is None or doc.app_id != app_id or doc.status == "deleting":
                raise DocumentNotFoundError()
            app = await s.get(App, app_id)
            cfg = dict(chunk_config if chunk_config is not None else app.chunk_config)  # type: ignore[union-attr]
        parsed = await self._load(doc, None)
        result = split_document(parsed, ChunkOptions.from_mapping(cfg))
        return preview_result(result, title=doc.title, chunk_config=cfg, limit=limit)

    # ------------------------------------------------------------------ 构建
    async def _load(self, doc: Document, storage_key: str | None) -> ParsedDoc:
        key = storage_key or doc.storage_key
        if key:
            data = await self.storage.get(key)
            return parse_bytes(data, detect_file_type(_ext(key), data))
        if doc.source_uri:
            fetched = await fetch_url(doc.source_uri, allowlist=self.settings.url_fetch_allowlist)
            return parse_bytes(fetched.content, fetched.file_type)
        raise ParseError(message="文档没有可读取的来源")

    async def _purge_build(self, app_id: UUID, document_id: UUID, n: int) -> None:
        await self.store.delete_document(app_id, document_id, index_version=n)
        async with self.sessions() as s:
            await ChunkRepository(s).delete_versions(app_id, document_id, index_version=n)
            await ChunkParentRepository(s).delete_versions(app_id, document_id, index_version=n)
            await s.commit()

    async def _cancelled(self, app_id: UUID, document_id: UUID) -> bool:
        async with self.sessions() as s:
            app = await s.get(App, app_id)
            doc = await s.get(Document, document_id)
            return (
                app is None or doc is None or app.status == "deleting" or doc.status == "deleting"
            )

    async def _progress(
        self, document_id: UUID, job_id: UUID, progress: int, status: str | None = None
    ) -> None:
        async with self.sessions() as s:
            doc = await s.get(Document, document_id)
            job = await s.get(Job, job_id)
            if doc is not None and doc.status != "deleting":
                doc.progress = progress
                if status:
                    doc.status = status
            if job is not None:
                job.progress = progress
            await s.commit()

    async def run_build(self, document_id: UUID, job_id: UUID) -> str:
        """执行一次构建，返回 job 最终状态（done / failed / cancelled / skipped）。可重复执行。"""
        async with self.sessions() as s:
            job = await s.get(Job, job_id)
            doc = await s.get(Document, document_id)
            if job is None or doc is None:
                return "skipped"
            if job.status in ("done", "failed", "cancelled"):
                return "skipped"  # 重复投递：已有终态
            app = await s.get(App, doc.app_id)
            n = doc.pending_index_version
            if app is None or app.status == "deleting" or doc.status == "deleting":
                job.status, job.finished_at = "cancelled", _now()
                await s.commit()
                return "cancelled"
            if n is None:
                job.status, job.error, job.finished_at = "failed", "没有待构建的版本", _now()
                await s.commit()
                return "failed"
            job.status, job.started_at, job.progress, job.error = "running", _now(), 0, None
            doc.status, doc.progress, doc.error = "parsing", 0, None
            await s.commit()
            app_id, title = doc.app_id, doc.title
            cfg: dict[str, Any] = dict(app.chunk_config)
            cfg_version, custom_dict = app.chunk_config_version, list(app.custom_dict or [])
            payload: dict[str, Any] = dict(job.payload or {})
        replace_key: str | None = payload.get("storage_key")

        try:
            await self._purge_build(app_id, document_id, n)  # 上次中断留下的半成品
            parsed = await self._load(doc, replace_key)
            result = split_document(parsed, ChunkOptions.from_mapping(cfg))
            if not result.chunks:
                raise ParseError(message="文档没有可切分的正文")
            await self._progress(document_id, job_id, PROGRESS_SPLIT_DONE, "indexing")

            items = await self._write_rows(app_id, document_id, n, result, title, cfg, custom_dict)

            span = 100 - PROGRESS_SPLIT_DONE

            async def before_batch(done: int, total: int) -> None:
                if await self._cancelled(app_id, document_id):
                    raise _Cancelled()
                if done:
                    await self._progress(
                        document_id, job_id, PROGRESS_SPLIT_DONE + span * done // total
                    )

            await self.store.ensure_chunks_collection()
            await embed_and_upsert(
                app_id=app_id,
                document_id=document_id,
                index_version=n,
                items=items,
                embedder=self.embedder,
                store=self.store,
                batch_size=self.settings.embed_batch * EMBED_PROGRESS_BATCHES,
                before_batch=before_batch,
            )
            old_key = await self._switch(
                document_id, job_id, n, len(items), cfg_version, title, payload
            )
        except _Cancelled:
            await self._safe_purge(app_id, document_id, n)
            await self._finish_job(job_id, "cancelled", None)
            return "cancelled"
        except Exception as exc:  # noqa: BLE001 - 任何失败都要落状态
            msg = exc.message if isinstance(exc, AppError) else f"{type(exc).__name__}: {exc}"
            log.warning(
                "ingest build failed", extra={"document_id": str(document_id), "error": msg}
            )
            await self._safe_purge(app_id, document_id, n)
            await self._fail(document_id, job_id, n, msg[:1000])
            if replace_key:
                await self.storage.delete(replace_key)
            return "failed"

        if self.enqueue_cleanup is not None:
            r = self.enqueue_cleanup(document_id, n, old_key)
            if r is not None:
                await r
        return "done"

    async def _write_rows(
        self,
        app_id: UUID,
        document_id: UUID,
        n: int,
        result: ChunkResult,
        title: str,
        cfg: Mapping[str, Any],
        custom_dict: list[str],
    ) -> list[EmbedItem]:
        parent_ids = {p.ordinal: new_id() for p in result.parents}
        parents = [
            ChunkParent(
                id=parent_ids[p.ordinal],
                app_id=app_id,
                document_id=document_id,
                index_version=n,
                ordinal=p.ordinal,
                heading_path=p.heading_path or None,
                text=p.text,
                tokens=p.tokens,
                page_from=p.page_from,
                page_to=p.page_to,
            )
            for p in result.parents
        ]
        chunks: list[Chunk] = []
        items: list[EmbedItem] = []
        for c in result.chunks:
            cid = new_id()
            pid = parent_ids.get(c.parent_ordinal) if c.parent_ordinal is not None else None
            chunks.append(
                Chunk(
                    id=cid,
                    app_id=app_id,
                    document_id=document_id,
                    index_version=n,
                    ordinal=c.ordinal,
                    parent_id=pid,
                    heading_path=c.heading_path or None,
                    text=c.text,
                    search_text=tokenize_for_index(
                        app_id, f"{c.heading_path} {c.text}".strip(), custom_dict
                    ),
                    tokens=c.tokens,
                    page_from=c.page_from,
                    page_to=c.page_to,
                )
            )
            items.append(EmbedItem(cid, pid, _embed_text_for(title, cfg, c.heading_path, c.text)))
        async with self.sessions() as s:
            if parents:
                await ChunkParentRepository(s).add_many(app_id, parents)
            await ChunkRepository(s).add_many(app_id, chunks)
            await s.commit()
        return items

    async def _switch(
        self,
        document_id: UUID,
        job_id: UUID,
        n: int,
        count: int,
        cfg_version: int,
        title: str,
        payload: Mapping[str, Any],
    ) -> str | None:
        """一个事务内切换生效版本；返回需要清理的旧文件 key（替换文件时）。"""
        async with self.sessions() as s:
            doc = await s.get(Document, document_id, with_for_update=True)
            if doc is None or doc.status == "deleting" or doc.pending_index_version != n:
                raise _Cancelled()
            old_key: str | None = None
            if payload.get("storage_key"):
                old_key = doc.storage_key
                doc.storage_key = payload["storage_key"]
                for f in ("content_hash", "mime_type", "size_bytes", "version"):
                    if f in payload:
                        setattr(doc, f, payload[f])
            doc.index_version, doc.pending_index_version = n, None
            doc.status, doc.progress, doc.error, doc.chunk_count = "done", 100, None, count
            doc.chunk_config_version, doc.embed_model = cfg_version, self.settings.embed_model
            doc.indexed_title = title
            job = await s.get(Job, job_id)
            if job is not None:
                job.status, job.progress, job.finished_at = "done", 100, _now()
            try:
                await s.commit()
            except IntegrityError as exc:  # 替换后内容与同应用其他文档重复
                raise ParseError(message="替换后的内容与已有文档重复") from exc
            return old_key if old_key != doc.storage_key else None

    async def _safe_purge(self, app_id: UUID, document_id: UUID, n: int) -> None:
        try:
            await self._purge_build(app_id, document_id, n)
        except Exception:  # noqa: BLE001 - 下次构建开头会再清
            log.exception("purge partial build failed", extra={"document_id": str(document_id)})

    async def _finish_job(self, job_id: UUID, status: str, error: str | None) -> None:
        async with self.sessions() as s:
            job = await s.get(Job, job_id)
            if job is not None:
                job.status, job.error, job.finished_at = status, error, _now()
                await s.commit()

    async def _fail(self, document_id: UUID, job_id: UUID, n: int, msg: str) -> None:
        async with self.sessions() as s:
            doc = await s.get(Document, document_id)
            if doc is not None and doc.status != "deleting":
                # 旧版本（index_version）保持生效、可检索；状态显示失败与原因
                doc.status, doc.error = "failed", msg
                if doc.pending_index_version == n:
                    doc.pending_index_version = None
            job = await s.get(Job, job_id)
            if job is not None:
                job.status, job.error, job.finished_at = "failed", msg, _now()
            await s.commit()

    # ------------------------------------------------------------------ 清理
    async def run_cleanup(
        self,
        document_id: UUID,
        *,
        below_index_version: int | None = None,
        old_storage_key: str | None = None,
    ) -> str:
        """清理旧版本；文档处于 deleting 时删除全部数据与文档行（文档处理流水线 §6）。"""
        async with self.sessions() as s:
            doc = await s.get(Document, document_id)
            if doc is None:
                return "skipped"
            app_id, deleting = doc.app_id, doc.status == "deleting"
        if deleting:
            await self.store.delete_document(app_id, document_id)
            async with self.sessions() as s:
                await ChunkRepository(s).delete_versions(app_id, document_id)
                await ChunkParentRepository(s).delete_versions(app_id, document_id)
                doc = await s.get(Document, document_id)
                keys = [k for k in (doc.storage_key if doc else None, old_storage_key) if k]
                if doc is not None:
                    await s.delete(doc)
                await s.commit()
            for k in keys:
                await self.storage.delete(k)
            return "deleted"
        if below_index_version is None:
            return "skipped"
        await self.store.delete_document(
            app_id, document_id, below_index_version=below_index_version
        )
        async with self.sessions() as s:
            await ChunkRepository(s).delete_versions(
                app_id, document_id, below_index_version=below_index_version
            )
            await ChunkParentRepository(s).delete_versions(
                app_id, document_id, below_index_version=below_index_version
            )
            await s.commit()
        if old_storage_key:
            await self.storage.delete(old_storage_key)
        return "cleaned"


# ---------------------------------------------------------------------- 任务入口
@asynccontextmanager
async def ingest_runtime(
    settings: Settings | None = None,
    *,
    enqueue_cleanup: CleanupEnqueuer | None = None,
    embedder: EmbeddingProvider | None = None,
) -> AsyncIterator[IngestService]:
    """为单个 Celery 任务创建独立的连接（引擎用 NullPool，结束时释放）。"""
    from sqlalchemy.pool import NullPool

    from aics.providers import build_embedder

    s = settings or get_settings()
    engine = create_async_engine(s.db_url, poolclass=NullPool)
    store = build_vector_store(s)
    try:
        yield IngestService(
            async_sessionmaker(engine, expire_on_commit=False),
            storage=build_storage(s),
            store=store,
            embedder=embedder or build_embedder(s),
            settings=s,
            enqueue_cleanup=enqueue_cleanup,
        )
    finally:
        await store.aclose()
        await engine.dispose()


async def run_ingest_job(
    document_id: UUID, job_id: UUID, *, enqueue_cleanup: CleanupEnqueuer | None = None
) -> str:
    async with ingest_runtime(enqueue_cleanup=enqueue_cleanup) as svc:
        return await svc.run_build(document_id, job_id)


async def run_cleanup_job(
    document_id: UUID, below_index_version: int | None, old_storage_key: str | None
) -> str:
    async with ingest_runtime() as svc:
        return await svc.run_cleanup(
            document_id, below_index_version=below_index_version, old_storage_key=old_storage_key
        )
