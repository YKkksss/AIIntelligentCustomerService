"""documents、chunks、jobs 的 repository（均继承 AppScopedRepository）。业务逻辑在 M1.4–M1.6 补充。"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select

from aics.db.models import Chunk, ChunkParent, Document, Job
from aics.db.scoped import AppScopedRepository


class DocumentRepository(AppScopedRepository[Document]):
    model = Document

    async def get_by_hash(self, app_id: UUID, content_hash: str) -> Document | None:
        stmt = self._scoped(app_id).where(
            Document.content_hash == content_hash, Document.deleted_at.is_(None)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()


class ChunkRepository(AppScopedRepository[Chunk]):
    model = Chunk

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
