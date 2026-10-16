"""knowledge 服务（M1.5 部分）：上传去重建档、请求重新分词。

上传 / 替换 / 删除 / 重建的完整流程与路由在 M1.6 补齐；任务按任务名投递（`enqueue(task_name, args)`），
本模块不 import `aics.tasks`。
"""

from __future__ import annotations

import hashlib
import posixpath
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from aics.core.config import get_settings
from aics.core.errors import AppError, NotFoundError, ValidationAppError
from aics.db.models import App, Document, Job
from aics.ingest.parsers import MIME_TYPES, detect_file_type
from aics.modules.knowledge.repository import DocumentRepository, JobRepository
from aics.storage import Storage
from aics.storage.base import document_key

INGEST_TASK = "aics.ingest_document"
RETOKENIZE_TASK = "aics.retokenize_app"
TITLE_MAX_CHARS = 200

Enqueue = Callable[[str, Sequence[Any]], None]


class DocumentBusyError(AppError):
    code, status, message = "DOCUMENT_BUSY", 409, "相同内容的文档正在删除，请稍后再上传"


@dataclass(frozen=True)
class UploadResult:
    document: Document
    duplicate: bool  # true：同应用已有相同内容（SHA-256），返回已有文档，不建任务
    job: Job | None


def content_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class KnowledgeService:
    def __init__(
        self, session: AsyncSession, *, storage: Storage, enqueue: Enqueue | None = None
    ) -> None:
        self.session = session
        self.storage = storage
        self.enqueue = enqueue
        self.docs = DocumentRepository(session)
        self.jobs = JobRepository(session)

    async def _app(self, app_id: UUID) -> App:
        app = await self.session.get(App, app_id)
        if app is None or app.status == "deleting":
            raise NotFoundError(code="APP_NOT_FOUND", message="应用不存在")
        return app

    async def _existing(self, app_id: UUID, digest: str) -> UploadResult | None:
        doc = await self.docs.get_by_hash(app_id, digest)
        if doc is None:
            return None
        if doc.status == "deleting":
            raise DocumentBusyError()
        return UploadResult(doc, True, None)

    async def upload_file(
        self,
        app_id: UUID,
        data: bytes,
        filename: str,
        *,
        title: str | None = None,
        created_by: UUID | None = None,
    ) -> UploadResult:
        """FR-KB-03：同应用按 SHA-256 去重，重复上传返回已有文档（`duplicate=true`）。

        先查后插；并发上传同一文件时由唯一索引 `(app_id, content_hash) WHERE deleted_at IS NULL`
        兜底：插入冲突 → 回滚、删除刚写入的文件、返回已有文档。提交由本方法完成。
        """
        await self._app(app_id)
        max_bytes = self._max_bytes()
        if not data:
            raise ValidationAppError(message="文件为空")
        if len(data) > max_bytes:
            raise ValidationAppError(message=f"文件超过 {max_bytes // (1024 * 1024)} MB")
        ftype = detect_file_type(filename, data)  # 扩展名白名单 + 魔数（不支持 → 415/422）
        digest = content_hash(data)
        found = await self._existing(app_id, digest)
        if found is not None:
            return found

        name = posixpath.basename(filename.replace("\\", "/")) or f"document.{ftype}"
        doc = Document(
            app_id=app_id,
            title=(title or name).strip()[:TITLE_MAX_CHARS] or name,
            source_type="file",
            mime_type=MIME_TYPES[ftype],
            size_bytes=len(data),
            content_hash=digest,
            status="queued",
            pending_index_version=1,
            created_by=created_by,
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
        if self.enqueue is not None:
            self.enqueue(INGEST_TASK, [str(doc.id), str(job.id)])
        return UploadResult(doc, False, job)

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
        if self.enqueue is not None:
            self.enqueue(RETOKENIZE_TASK, [str(app_id), str(job.id)])
        return job

    @staticmethod
    def _max_bytes() -> int:
        return get_settings().upload_max_mb * 1024 * 1024
