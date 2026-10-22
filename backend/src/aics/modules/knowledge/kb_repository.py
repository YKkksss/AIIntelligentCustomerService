"""知识库与关联查询（M4.7 ②）。

`KnowledgeBaseRepository` 面向平台：库本身没有 `app_id`，所以不继承 `AppScopedRepository`；
`AppKBLinkRepository` 是「客服 ↔ 知识库」的关联表，带 `app_id`，按作用域基类实现。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any
from uuid import UUID

from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from aics.db.models.knowledge import AppKBLink, Chunk, Document, KnowledgeBase
from aics.db.scoped import MAX_PAGE_SIZE, AppScopedRepository


def kb_row(
    kb: KnowledgeBase,
    *,
    documents: int = 0,
    chunks: int = 0,
    apps: list[dict[str, str]] | None = None,
    linked: bool | None = None,
    is_default: bool | None = None,
) -> dict[str, Any]:
    """库的对外视图：不含创建人等内部字段。"""
    row: dict[str, Any] = {
        "id": str(kb.id),
        "name": kb.name,
        "description": kb.description,
        "status": kb.status,
        "chunk_config": dict(kb.chunk_config or {}),
        "config_version": kb.config_version,
        "document_count": documents,
        "chunk_count": chunks,
        "apps": apps or [],
        "created_at": kb.created_at,
        "updated_at": kb.updated_at,
    }
    if linked is not None:
        row["linked"] = linked
    if is_default is not None:
        row["is_default"] = is_default
    return row


class KnowledgeBaseRepository:
    """平台级知识库表；分页与计数在服务层组合。"""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list(
        self, *, status: str | None = None, limit: int = 20, offset: int = 0
    ) -> list[KnowledgeBase]:
        stmt = select(KnowledgeBase).order_by(KnowledgeBase.created_at.desc(), KnowledgeBase.id)
        if status:
            stmt = stmt.where(KnowledgeBase.status == status)
        stmt = stmt.limit(min(max(limit, 1), MAX_PAGE_SIZE)).offset(max(offset, 0))
        return list((await self.session.execute(stmt)).scalars())

    async def count(self, *, status: str | None = None) -> int:
        stmt = select(func.count()).select_from(KnowledgeBase)
        if status:
            stmt = stmt.where(KnowledgeBase.status == status)
        return int((await self.session.execute(stmt)).scalar_one())

    async def list_page(
        self,
        *,
        status: str | None = None,
        q: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[KnowledgeBase], int]:
        """名称/描述模糊搜索 + 状态过滤 + 分页；返回（本页行, 总数）。"""
        conditions = []
        if status:
            conditions.append(KnowledgeBase.status == status)
        if q:
            pattern = f"%{q}%"
            conditions.append(
                KnowledgeBase.name.ilike(pattern) | KnowledgeBase.description.ilike(pattern)
            )
        base = select(KnowledgeBase)
        count_stmt = select(func.count()).select_from(KnowledgeBase)
        if conditions:
            base = base.where(*conditions)
            count_stmt = count_stmt.where(*conditions)
        total = int((await self.session.execute(count_stmt)).scalar_one())
        size = min(max(int(page_size), 1), MAX_PAGE_SIZE)
        stmt = (
            base.order_by(KnowledgeBase.created_at.desc(), KnowledgeBase.id)
            .limit(size)
            .offset(max(page - 1, 0) * size)
        )
        return list((await self.session.execute(stmt)).scalars()), total

    async def by_name(self, name: str) -> KnowledgeBase | None:
        stmt = select(KnowledgeBase).where(KnowledgeBase.name == name)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get(self, kb_id: UUID) -> KnowledgeBase | None:
        return (
            await self.session.execute(select(KnowledgeBase).where(KnowledgeBase.id == kb_id))
        ).scalar_one_or_none()

    async def get_many(self, kb_ids: Sequence[UUID]) -> list[KnowledgeBase]:
        if not kb_ids:
            return []
        stmt = select(KnowledgeBase).where(KnowledgeBase.id.in_(list(kb_ids)))
        return list((await self.session.execute(stmt)).scalars())

    async def create(self, kb: KnowledgeBase) -> KnowledgeBase:
        self.session.add(kb)
        await self.session.flush()
        return kb

    async def save(self, kb_id: UUID, **values: Any) -> int:
        if "id" in values:
            raise ValueError("不能修改 id")
        stmt = update(KnowledgeBase).where(KnowledgeBase.id == kb_id).values(**values)
        return int((await self.session.execute(stmt)).rowcount)

    async def counts(self, kb_ids: Sequence[UUID]) -> dict[UUID, tuple[int, int]]:
        """一次取回每个库的（文档数, 切块数），供列表页展示。"""
        if not kb_ids:
            return {}
        ids = list(kb_ids)
        docs = dict(
            (
                await self.session.execute(
                    select(Document.kb_id, func.count())
                    .where(Document.kb_id.in_(ids), Document.deleted_at.is_(None))
                    .group_by(Document.kb_id)
                )
            ).all()
        )
        chunks = dict(
            (
                await self.session.execute(
                    select(Chunk.kb_id, func.count())
                    .where(Chunk.kb_id.in_(ids), Chunk.enabled.is_(True))
                    .group_by(Chunk.kb_id)
                )
            ).all()
        )
        return {kid: (int(docs.get(kid, 0)), int(chunks.get(kid, 0))) for kid in ids}


class AppKBLinkRepository(AppScopedRepository[AppKBLink]):
    """客服与知识库的关联；复合主键 (app_id, kb_id)。"""

    model = AppKBLink

    def _where_id(self, app_id: UUID, id: UUID) -> list[Any]:  # noqa: ARG002
        return [AppKBLink.app_id == app_id, AppKBLink.kb_id == id]

    async def kb_ids(self, app_id: UUID) -> list[UUID]:
        stmt = select(AppKBLink.kb_id).where(AppKBLink.app_id == app_id).order_by(AppKBLink.kb_id)
        return list((await self.session.execute(stmt)).scalars())

    async def replace(
        self,
        app_id: UUID,
        kb_ids: Iterable[UUID],
        *,
        created_by: UUID | None = None,
        default_kb_id: UUID | None = None,
    ) -> tuple[list[UUID], list[UUID]]:
        """全量替换关联；返回（新增, 移除）。"""
        wanted = list(dict.fromkeys(kb_ids))
        current = set(await self.kb_ids(app_id))
        target = set(wanted)
        added = [k for k in wanted if k not in current]
        removed = sorted(current - target)
        if removed:
            await self.session.execute(
                delete(AppKBLink).where(AppKBLink.app_id == app_id, AppKBLink.kb_id.in_(removed))
            )
        if added:
            await self.session.execute(
                insert(AppKBLink).values(
                    [
                        {"app_id": app_id, "kb_id": kb_id, "created_by": created_by}
                        for kb_id in added
                    ]
                )
            )
        # 默认库：显式指定 > 保留原默认 > 本次的第一条
        current_default = await self.default_kb_id(app_id)
        wanted_set = target
        if default_kb_id is not None and default_kb_id in wanted_set:
            keep: UUID | None = default_kb_id
        elif current_default is not None and current_default in wanted_set:
            keep = current_default
        else:
            keep = wanted[0] if wanted else None
        await self.mark_default(app_id, keep)
        return added, removed

    async def default_kb_id(self, app_id: UUID) -> UUID | None:
        stmt = select(AppKBLink.kb_id).where(
            AppKBLink.app_id == app_id, AppKBLink.is_default.is_(True)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def mark_default(self, app_id: UUID, kb_id: UUID | None) -> None:
        """把 `kb_id` 设为该应用唯一的默认库；传 None 表示清除。

        分两步写：先清掉旧的默认（部分唯一索引不允许两条同时为 true），再设新的。
        """
        await self.session.execute(
            update(AppKBLink)
            .where(AppKBLink.app_id == app_id, AppKBLink.is_default.is_(True))
            .values(is_default=False)
        )
        if kb_id is not None:
            await self.session.execute(
                update(AppKBLink)
                .where(AppKBLink.app_id == app_id, AppKBLink.kb_id == kb_id)
                .values(is_default=True)
            )

    async def apps_of_kb_all_apps(self, kb_id: UUID) -> list[UUID]:
        stmt = select(AppKBLink.app_id).where(AppKBLink.kb_id == kb_id)
        return list((await self.session.execute(stmt)).scalars())
