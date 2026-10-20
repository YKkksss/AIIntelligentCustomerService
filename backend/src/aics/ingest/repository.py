"""documents、chunks、chunk_parents、chunk_questions、jobs 的 repository（均继承 AppScopedRepository）。

放在 ingest（L2）中：入库流水线与 knowledge 模块共用；knowledge.repository 重新导出。
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import delete, func, select, update

from aics.db.models import Chunk, ChunkParent, ChunkQuestion, Document, Job
from aics.db.scoped import AppScopedRepository

MAX_PAGE_SIZE = 200  # 与 db.scoped 一致（接口层上限 100，见接口规范 §5）


def like_pattern(q: str) -> str:
    """`%` / `_` / `\` 转义后的 ILIKE 模式（配合 `escape="\\\\"`），用户输入不会变成通配符。"""
    escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


class DocumentRepository(AppScopedRepository[Document]):
    model = Document

    async def get_by_hash(self, app_id: UUID, content_hash: str) -> Document | None:
        stmt = self._scoped(app_id).where(
            Document.content_hash == content_hash, Document.deleted_at.is_(None)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def page_for_app(
        self,
        app_id: UUID,
        *,
        status: str | None = None,
        q: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[list[Document], int]:
        """开放接口的文档列表：按创建时间倒序，`q` 在标题上 ILIKE，软删除的文档不返回。

        返回 `(rows, total)`；`total` 是同一条件的总数（过滤后），供页码分页使用。
        """
        conds: list[object] = [Document.deleted_at.is_(None)]
        if status is not None:
            conds.append(Document.status == status)
        if q:
            conds.append(Document.title.ilike(like_pattern(q), escape="\\"))
        stmt = (
            self._scoped(app_id)
            .where(*conds)
            .order_by(Document.created_at.desc(), Document.id.desc())
            .limit(min(max(limit, 1), MAX_PAGE_SIZE))
            .offset(max(offset, 0))
        )
        rows = list((await self.session.execute(stmt)).scalars())
        count_stmt = (
            select(func.count()).select_from(Document).where(Document.app_id == app_id, *conds)
        )
        total = int((await self.session.execute(count_stmt)).scalar_one())
        return rows, total

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

    async def page_for_document(
        self,
        app_id: UUID,
        document_id: UUID,
        index_version: int,
        *,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[list[Chunk], int]:
        """切块列表（开放接口 §4）：只取生效构建号，按 `ordinal` 升序，返回 `(rows, total)`。"""
        conds: list[object] = [
            Chunk.document_id == document_id,
            Chunk.index_version == index_version,
        ]
        stmt = (
            self._scoped(app_id)
            .where(*conds)
            .order_by(Chunk.ordinal)
            .limit(min(max(limit, 1), MAX_PAGE_SIZE))
            .offset(max(offset, 0))
        )
        rows = list((await self.session.execute(stmt)).scalars())
        count_stmt = select(func.count()).select_from(Chunk).where(Chunk.app_id == app_id, *conds)
        total = int((await self.session.execute(count_stmt)).scalar_one())
        return rows, total

    async def page_for_app(
        self,
        app_id: UUID,
        *,
        document_id: UUID | None = None,
        review_status: str | None = None,
        enabled: bool | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[list[tuple[Chunk, str]], int]:
        """跨文档切块列表（M4.3 ③）：只取各文档生效构建号、未删除文档的切块。

        返回 `([(chunk, 文档标题)], total)`，按「文档 → 序号」升序。
        """
        conds: list[object] = [
            Chunk.index_version == Document.index_version,
            Document.deleted_at.is_(None),
            Document.status != "deleting",
        ]
        if document_id is not None:
            conds.append(Chunk.document_id == document_id)
        if review_status is not None:
            conds.append(Chunk.review_status == review_status)
        if enabled is not None:
            conds.append(Chunk.enabled.is_(enabled))
        stmt = (
            select(Chunk, Document.title)
            .join(Document, Document.id == Chunk.document_id)
            .where(Chunk.app_id == app_id, *conds)
            .order_by(Chunk.document_id, Chunk.ordinal)
            .limit(min(max(limit, 1), MAX_PAGE_SIZE))
            .offset(max(offset, 0))
        )
        rows = (await self.session.execute(stmt)).all()
        count_stmt = (
            select(func.count())
            .select_from(Chunk)
            .join(Document, Document.id == Chunk.document_id)
            .where(Chunk.app_id == app_id, *conds)
        )
        total = int((await self.session.execute(count_stmt)).scalar_one())
        return [(row[0], row[1]) for row in rows], total


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


class ChunkQuestionRepository(AppScopedRepository[ChunkQuestion]):
    model = ChunkQuestion

    async def list_for_document(
        self, app_id: UUID, document_id: UUID, *, status: str | None = None
    ) -> list[ChunkQuestion]:
        stmt = self._scoped(app_id).where(ChunkQuestion.document_id == document_id)
        if status is not None:
            stmt = stmt.where(ChunkQuestion.status == status)
        stmt = stmt.order_by(ChunkQuestion.anchor_ordinal, ChunkQuestion.created_at)
        return list((await self.session.execute(stmt)).scalars())

    async def list_for_chunk(self, app_id: UUID, chunk_id: UUID) -> list[ChunkQuestion]:
        stmt = (
            self._scoped(app_id)
            .where(ChunkQuestion.chunk_id == chunk_id)
            .order_by(ChunkQuestion.created_at)
        )
        return list((await self.session.execute(stmt)).scalars())

    async def count_for_chunk(self, app_id: UUID, chunk_id: UUID) -> int:
        stmt = select(func.count()).where(
            ChunkQuestion.app_id == app_id, ChunkQuestion.chunk_id == chunk_id
        )
        return int((await self.session.execute(stmt)).scalar_one())

    async def delete_one(self, app_id: UUID, question_id: UUID) -> bool:
        stmt = delete(ChunkQuestion).where(
            ChunkQuestion.app_id == app_id, ChunkQuestion.id == question_id
        )
        return ((await self.session.execute(stmt)).rowcount or 0) > 0  # type: ignore[attr-defined]


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
