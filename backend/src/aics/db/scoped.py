"""AppScopedRepository：所有带 app_id 的表的 repository 基类（模块划分 §5）。

规则：子类新增的公开方法第一个参数必须是 `app_id`，且只能基于 `_scoped()` 或带 app_id 条件的语句构造查询；
`*_all_apps` 命名的方法除外（只对 super_admin 开放，由 service 层把关）。单元测试遍历所有子类校验签名。
"""

from __future__ import annotations

import inspect
from collections.abc import Mapping
from typing import Any, ClassVar
from uuid import UUID

from sqlalchemy import Select, delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from aics.db.base import Base

MAX_PAGE_SIZE = 200


class AppScopedRepository[M: Base]:
    model: ClassVar[type[Base]]
    _registry: ClassVar[list[type[AppScopedRepository[Any]]]] = []

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        model = cls.__dict__.get("model")
        if model is not None and not hasattr(model, "app_id"):
            raise TypeError(f"{cls.__name__}.model={model.__name__} 没有 app_id 列")
        AppScopedRepository._registry.append(cls)

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ---- 查询起点 ----
    def _scoped(self, app_id: UUID) -> Select[tuple[M]]:
        return select(self.model).where(self.model.app_id == app_id)  # type: ignore[attr-defined]

    def _where_id(self, app_id: UUID, id: UUID) -> list[Any]:
        m = self.model
        return [m.app_id == app_id, m.id == id]  # type: ignore[attr-defined]

    # ---- 通用 CRUD ----
    async def get(self, app_id: UUID, id: UUID) -> M | None:
        stmt = self._scoped(app_id).where(self.model.id == id)  # type: ignore[attr-defined]
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list(
        self,
        app_id: UUID,
        *,
        filters: Mapping[str, Any] | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> list[M]:
        stmt = self._scoped(app_id)
        for col, val in (filters or {}).items():
            if col == "app_id":
                raise ValueError("app_id 不能作为 filters 传入")
            stmt = stmt.where(getattr(self.model, col) == val)
        stmt = stmt.limit(min(max(limit, 1), MAX_PAGE_SIZE)).offset(max(offset, 0))
        return list((await self.session.execute(stmt)).scalars())

    async def count(self, app_id: UUID) -> int:
        stmt = (
            select(func.count())
            .select_from(self.model)
            .where(
                self.model.app_id == app_id  # type: ignore[attr-defined]
            )
        )
        return int((await self.session.execute(stmt)).scalar_one())

    async def add(self, app_id: UUID, obj: M) -> M:
        if obj.app_id != app_id:  # type: ignore[attr-defined]
            raise ValueError("对象的 app_id 与参数不一致")
        self.session.add(obj)
        await self.session.flush()
        return obj

    async def update(self, app_id: UUID, id: UUID, /, **values: Any) -> int:
        if "app_id" in values or "id" in values:
            raise ValueError("不能修改 app_id / id")
        stmt = update(self.model).where(*self._where_id(app_id, id)).values(**values)
        return int((await self.session.execute(stmt)).rowcount)  # type: ignore[attr-defined]

    async def delete(self, app_id: UUID, id: UUID) -> int:
        stmt = delete(self.model).where(*self._where_id(app_id, id))
        return int((await self.session.execute(stmt)).rowcount)  # type: ignore[attr-defined]


def unscoped_methods(cls: type[AppScopedRepository[Any]]) -> list[str]:
    """返回第一个参数不是 app_id 的公开方法名（供测试与 CI 校验）。"""
    bad: list[str] = []
    for name, fn in inspect.getmembers(cls, inspect.isfunction):
        if name.startswith("_") or name.endswith("_all_apps"):
            continue
        params = [p for p in inspect.signature(fn).parameters if p != "self"]
        if not params or params[0] != "app_id":
            bad.append(name)
    return bad


def registered_repositories() -> list[type[AppScopedRepository[Any]]]:
    return list(AppScopedRepository._registry)
