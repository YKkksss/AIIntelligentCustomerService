"""documents、chunks、chunk_parents、jobs 的 repository（均继承 AppScopedRepository）。

放在 ingest（L2）中：入库流水线与 knowledge 模块共用；knowledge.repository 重新导出。
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import delete, func, select, update

from aics.db.models import Chunk, ChunkParent, Document, Job
from aics.db.scoped import AppScopedRepository


class DocumentRepository(AppScopedRepository[Document]):
    model = Document

    async def get_by_hash(self, app_id: UUID, content_hash: str) -> Document | None:
        stmt = self._scoped(app_id).where(
            Document.content_hash == content_hash, Document.deleted_at.is_(None)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_for_update(self, app_id: UUID, document_id: UUID) -> Document | None:
        stmt = self._scoped(app_id).where(Document.id == document_id).with_for_update()
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_not_deleting(self, app_id: UUID) -> list[Document]:
        stmt = (
            self._scoped(app_id).where(Document.status != "deleting").order_by(Document.created_at)
        )
        return list((await self.session.execute(stmt)).scalars())


def _version_filter(
    model: type[Chunk] | type[ChunkParent], index_version: int | None, below: int | None
) -> list[object]:
    if index_version is not None:
        return [model.index_version == index_version]
    if below is not None:
        return [model.index_version < below]
    return []


class ChunkRepository(AppScopedRepository[Chunk]):
    model = Chunk

    async def add_many(self, app_id: UUID, rows: list[Chunk]) -> None:
        if any(r.app_id != app_id for r in rows):
            raise ValueError("切块的 app_id 与参数不一致")
        self.session.add_all(rows)
        await self.session.flush()

    async def disable_document(self, app_id: UUID, document_id: UUID) -> int:
        stmt = (
            update(Chunk)
            .where(Chunk.app_id == app_id, Chunk.document_id == document_id, Chunk.enabled)
            .values(enabled=False)
        )
        return int((await self.session.execute(stmt)).rowcount)  # type: ignore[attr-defined]

    async def count_for_build(self, app_id: UUID, document_id: UUID, index_version: int) -> int:
        stmt = (
            select(func.count())
            .select_from(Chunk)
            .where(
                Chunk.app_id == app_id,
                Chunk.document_id == document_id,
                Chunk.index_version == index_version,
            )
        )
        return int((await self.session.execute(stmt)).scalar_one())

    async def delete_versions(
        self,
        app_id: UUID,
        document_id: UUID,
        *,
        index_version: int | None = None,
        below_index_version: int | None = None,
    ) -> int:
        """删除某构建号（清半成品）/ 低于某构建号（清旧版本）/ 全部（都不给）的切块。"""
        stmt = delete(Chunk).where(
            Chunk.app_id == app_id,
            Chunk.document_id == document_id,
            *_version_filter(Chunk, index_version, below_index_version),
        )
        return int((await self.session.execute(stmt)).rowcount)  # type: ignore[attr-defined]

    async def list_for_document(
        self, app_id: UUID, document_id: UUID, index_version: int
    ) -> list[Chunk]:
        stmt = (
            self._scoped(app_id)
            .where(Chunk.document_id == document_id, Chunk.index_version == index_version)
            .order_by(Chunk.ordinal)
        )
        return list((await self.session.execute(stmt)).scalars())


class ChunkParentRepository(AppScopedRepository[ChunkParent]):
    model = ChunkParent

    async def add_many(self, app_id: UUID, rows: list[ChunkParent]) -> None:
        if any(r.app_id != app_id for r in rows):
            raise ValueError("父块的 app_id 与参数不一致")
        self.session.add_all(rows)
        await self.session.flush()

    async def delete_versions(
        self,
        app_id: UUID,
        document_id: UUID,
        *,
        index_version: int | None = None,
        below_index_version: int | None = None,
    ) -> int:
        stmt = delete(ChunkParent).where(
            ChunkParent.app_id == app_id,
            ChunkParent.document_id == document_id,
            *_version_filter(ChunkParent, index_version, below_index_version),
        )
        return int((await self.session.execute(stmt)).rowcount)  # type: ignore[attr-defined]

    async def get_many(self, app_id: UUID, ids: list[UUID]) -> dict[UUID, ChunkParent]:
        """检索命中子块后批量取父块正文（RAG 流程设计 §4.1）。"""
        if not ids:
            return {}
        stmt = self._scoped(app_id).where(ChunkParent.id.in_(ids))
        return {p.id: p for p in (await self.session.execute(stmt)).scalars()}


class JobRepository(AppScopedRepository[Job]):
    model = Job

    async def add_platform_job_all_apps(self, job: Job) -> Job:
        """平台级任务（app_id 为 NULL，如 reindex_all），只由 super_admin 触发。"""
        if job.app_id is not None:
            raise ValueError("平台级任务的 app_id 必须为空")
        self.session.add(job)
        await self.session.flush()
        return job

    async def list_platform_all_apps(self, limit: int = 20) -> list[Job]:
        stmt = select(Job).where(Job.app_id.is_(None)).order_by(Job.created_at.desc()).limit(limit)
        return list((await self.session.execute(stmt)).scalars())
